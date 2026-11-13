import glob
import os
import shutil
import xml.etree.ElementTree as ET
from pythonforandroid.toolchain import info


ANDROID_NS = "http://schemas.android.com/apk/res/android"


def _attr(name: str) -> str:
    return f"{{{ANDROID_NS}}}{name}"


def _find_dist_dirs(toolchain, project_dir: str) -> list[str]:
    """
    يكتشف مسار مجلد التوزيعة (dist_dir) الخاص بـ python-for-android بدقة
    بغض النظر عن اختلاف إصدار p4a.
    """
    candidates = []

    for attr_chain in [
        ("dist", "dist_dir"),
        ("_dist", "dist_dir"),
        ("ctx", "bootstrap", "dist_dir"),
    ]:
        obj = toolchain
        for key in attr_chain:
            obj = getattr(obj, key, None)
            if obj is None:
                break
        if isinstance(obj, str) and os.path.isdir(obj):
            candidates.append(obj)

    if not candidates:
        pattern = os.path.join(
            project_dir, ".buildozer", "android", "platform", "build-*", "dists", "*"
        )
        for path in glob.glob(pattern):
            if os.path.isdir(path):
                candidates.append(path)

    # إزالة التكرار مع الحفاظ على الترتيب
    return list(dict.fromkeys(candidates))


def _inject_into_dist(dist_dir: str, project_dir: str):
    # 1. نسخ ملفات res/xml إلى مجلد موارد Gradle
    src_xml = os.path.join(project_dir, "res", "xml")
    dst_xml = os.path.join(dist_dir, "src", "main", "res", "xml")
    if os.path.isdir(src_xml):
        os.makedirs(dst_xml, exist_ok=True)
        for fname in os.listdir(src_xml):
            if fname.endswith(".xml"):
                shutil.copy2(os.path.join(src_xml, fname), os.path.join(dst_xml, fname))

    # 2. إضافة وصف خدمة إمكانية الوصول إلى strings.xml
    strings_candidates = [
        os.path.join(dist_dir, "src", "main", "res", "values", "strings.xml"),
        os.path.join(dist_dir, "res", "values", "strings.xml"),
    ]
    for strings_path in strings_candidates:
        if os.path.exists(strings_path):
            tree = ET.parse(strings_path)
            root = tree.getroot()
            exists = any(
                el.attrib.get("name") == "accessibility_service_description"
                for el in root.findall("string")
            )
            if not exists:
                s_el = ET.SubElement(
                    root, "string", {"name": "accessibility_service_description"}
                )
                s_el.text = (
                    "مراقبة التطبيق المفتوح وحماية تطبيق الحظر المشروط "
                    "من الإيقاف أو الحذف أثناء فترة القفل الصارم."
                )
                tree.write(strings_path, encoding="utf-8", xml_declaration=True)

    # 3. تعديل AndroidManifest.xml وحقن الخدمات والمستقبلات الأصلية
    manifest_candidates = [
        os.path.join(dist_dir, "src", "main", "AndroidManifest.xml"),
        os.path.join(dist_dir, "AndroidManifest.xml"),
    ]
    ET.register_namespace("android", ANDROID_NS)

    for manifest_path in manifest_candidates:
        if not os.path.exists(manifest_path):
            continue

        tree = ET.parse(manifest_path)
        root = tree.getroot()
        app_el = root.find("application")
        if app_el is None:
            continue

        # ضبط foregroundServiceType لخدمة المراقبة لتوافق Android 14 (API 34)
        for el in app_el.findall("service"):
            svc_name = el.attrib.get(_attr("name"), "")
            if "ServiceMonitor" in svc_name:
                el.set(_attr("foregroundServiceType"), "specialUse")
                el.set(_attr("exported"), "false")
                has_prop = any(
                    p.attrib.get(_attr("name")) == "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                    for p in el.findall("property")
                )
                if not has_prop:
                    ET.SubElement(
                        el,
                        "property",
                        {
                            _attr("name"): "android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE",
                            _attr("value"): "Conditional app usage monitoring and strict lock enforcement.",
                        },
                    )

        existing_names = {
            el.attrib.get(_attr("name"))
            for el in list(app_el)
            if _attr("name") in el.attrib
        }

        # حقن AccessibilityService
        acc_name = "org.guardian.condblocker.AppBlockerAccessibilityService"
        if acc_name not in existing_names:
            svc = ET.SubElement(
                app_el,
                "service",
                {
                    _attr("name"): acc_name,
                    _attr("permission"): "android.permission.BIND_ACCESSIBILITY_SERVICE",
                    _attr("exported"): "true",
                },
            )
            ifilter = ET.SubElement(svc, "intent-filter")
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.accessibilityservice.AccessibilityService"},
            )
            ET.SubElement(
                svc,
                "meta-data",
                {
                    _attr("name"): "android.accessibilityservice",
                    _attr("resource"): "@xml/accessibility_service_config",
                },
            )

        # حقن DeviceAdminReceiver
        admin_name = "org.guardian.condblocker.AppDeviceAdminReceiver"
        if admin_name not in existing_names:
            rec = ET.SubElement(
                app_el,
                "receiver",
                {
                    _attr("name"): admin_name,
                    _attr("permission"): "android.permission.BIND_DEVICE_ADMIN",
                    _attr("exported"): "true",
                },
            )
            ET.SubElement(
                rec,
                "meta-data",
                {
                    _attr("name"): "android.app.device_admin",
                    _attr("resource"): "@xml/device_admin_policies",
                },
            )
            ifilter = ET.SubElement(rec, "intent-filter")
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.app.action.DEVICE_ADMIN_ENABLED"},
            )
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.app.action.DEVICE_ADMIN_DISABLE_REQUESTED"},
            )

        # حقن BootReceiver
        boot_name = "org.guardian.condblocker.BootReceiver"
        if boot_name not in existing_names:
            brec = ET.SubElement(
                app_el,
                "receiver",
                {
                    _attr("name"): boot_name,
                    _attr("enabled"): "true",
                    _attr("exported"): "true",
                },
            )
            ifilter = ET.SubElement(brec, "intent-filter")
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.intent.action.BOOT_COMPLETED"},
            )
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.intent.action.LOCKED_BOOT_COMPLETED"},
            )
            ET.SubElement(
                ifilter,
                "action",
                {_attr("name"): "android.intent.action.QUICKBOOT_POWERON"},
            )

        tree.write(manifest_path, encoding="utf-8", xml_declaration=True)
        info(f"CondBlocker p4a_hook: updated {manifest_path}")


def before_apk_build(toolchain):
    project_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dirs = _find_dist_dirs(toolchain, project_dir)
    if not dist_dirs:
        info("CondBlocker p4a_hook: dist_dir not found yet.")
        return
    for d in dist_dirs:
        _inject_into_dist(d, project_dir)


def after_apk_build(toolchain):
    pass
