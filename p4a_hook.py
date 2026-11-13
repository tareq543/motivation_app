import glob
import os
import shutil
import xml.etree.ElementTree as ET
from pythonforandroid.toolchain import info


ANDROID_NS = "http://schemas.android.com/apk/res/android"

MANIFEST_INJECTION = """
    <service
        android:name="org.guardian.condblocker.AppBlockerAccessibilityService"
        android:permission="android.permission.BIND_ACCESSIBILITY_SERVICE"
        android:exported="true">
        <intent-filter>
            <action android:name="android.accessibilityservice.AccessibilityService" />
        </intent-filter>
        <meta-data
            android:name="android.accessibilityservice"
            android:resource="@xml/accessibility_service_config" />
    </service>

    <receiver
        android:name="org.guardian.condblocker.AppDeviceAdminReceiver"
        android:permission="android.permission.BIND_DEVICE_ADMIN"
        android:exported="true">
        <meta-data
            android:name="android.app.device_admin"
            android:resource="@xml/device_admin_policies" />
        <intent-filter>
            <action android:name="android.app.action.DEVICE_ADMIN_ENABLED" />
            <action android:name="android.app.action.DEVICE_ADMIN_DISABLE_REQUESTED" />
        </intent-filter>
    </receiver>

    <receiver
        android:name="org.guardian.condblocker.BootReceiver"
        android:enabled="true"
        android:exported="true">
        <intent-filter>
            <action android:name="android.intent.action.BOOT_COMPLETED" />
            <action android:name="android.intent.action.LOCKED_BOOT_COMPLETED" />
            <action android:name="android.intent.action.QUICKBOOT_POWERON" />
        </intent-filter>
    </receiver>
"""

CONDBLOCKER_STRINGS_XML = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="accessibility_service_description">مراقبة التطبيق المفتوح وحماية تطبيق الحظر المشروط من الإيقاف أو الحذف أثناء فترة القفل الصارم.</string>
</resources>
"""


def _find_dist_dirs(toolchain, project_dir: str) -> list[str]:
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

    return list(dict.fromkeys(candidates))


def _inject_into_dist(dist_dir: str, project_dir: str):
    # 1. نسخ ملفات res/xml إلى مجلد موارد التطبيق
    src_xml = os.path.join(project_dir, "res", "xml")
    dst_xml = os.path.join(dist_dir, "src", "main", "res", "xml")
    if os.path.isdir(src_xml):
        os.makedirs(dst_xml, exist_ok=True)
        for fname in os.listdir(src_xml):
            if fname.endswith(".xml"):
                shutil.copy2(os.path.join(src_xml, fname), os.path.join(dst_xml, fname))

    # 2. إنشاء ملف condblocker_strings.xml مستقل حتى لا يمسحه p4a عند توليد strings.xml
    values_dir = os.path.join(dist_dir, "src", "main", "res", "values")
    os.makedirs(values_dir, exist_ok=True)
    custom_strings_path = os.path.join(values_dir, "condblocker_strings.xml")
    with open(custom_strings_path, "w", encoding="utf-8") as f:
        f.write(CONDBLOCKER_STRINGS_XML)

    # 3. حقن الخدمات والمستقبلات في قالب AndroidManifest.tmpl.xml قبل أن يولده p4a
    tmpl_candidates = [
        os.path.join(dist_dir, "templates", "AndroidManifest.tmpl.xml"),
        os.path.join(dist_dir, "src", "main", "AndroidManifest.xml"),
    ]
    for tmpl_path in tmpl_candidates:
        if os.path.exists(tmpl_path):
            with open(tmpl_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "AppBlockerAccessibilityService" not in content and "</application>" in content:
                content = content.replace(
                    "</application>",
                    f"{MANIFEST_INJECTION}\n</application>",
                    1,
                )
                with open(tmpl_path, "w", encoding="utf-8") as f:
                    f.write(content)
                info(f"CondBlocker p4a_hook: injected components into {tmpl_path}")


def before_apk_build(toolchain):
    project_dir = os.path.dirname(os.path.abspath(__file__))
    dist_dirs = _find_dist_dirs(toolchain, project_dir)
    for d in dist_dirs:
        _inject_into_dist(d, project_dir)


def after_apk_build(toolchain):
    pass
