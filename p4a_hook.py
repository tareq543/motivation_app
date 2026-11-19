import glob
import os
import re
import shutil


MANIFEST_INJECTION = """
    <service
        android:name="com.tareq.motiv_force.AppBlockerAccessibilityService"
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
        android:name="com.tareq.motiv_force.AppDeviceAdminReceiver"
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
        android:name="com.tareq.motiv_force.BootReceiver"
        android:enabled="true"
        android:exported="true"
        android:directBootAware="true">
        <intent-filter>
            <action android:name="android.intent.action.BOOT_COMPLETED" />
            <action android:name="android.intent.action.LOCKED_BOOT_COMPLETED" />
            <action android:name="android.intent.action.QUICKBOOT_POWERON" />
        </intent-filter>
    </receiver>
"""

SERVICE_MONITOR_TMPL_RE = re.compile(
    r'<service\s+android:name="\{\{\s*args\.package\s*\}\}\.Service\{\{\s*([^}]+)\s*\}\}"\s+android:process=":service_\{\{\s*([^}]+)\s*\}\}"[^>]*(?:/>|>\s*</service>)',
    re.DOTALL,
)


def _replace_tmpl_service(match):
    cap_expr = match.group(1).strip()
    raw_expr = match.group(2).strip()
    return (
        f'<service android:name="{{{{ args.package }}}}.Service{{{{ {cap_expr} }}}}"\n'
        f'             android:process=":service_{{{{ {raw_expr} }}}}"\n'
        f'             android:foregroundServiceType="specialUse"\n'
        f'             android:exported="false">\n'
        f'        <property\n'
        f'            android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"\n'
        f'            android:value="Monitoring foreground app usage to enforce user-configured screen time limits" />\n'
        f'    </service>'
    )
SERVICE_MONITOR_XML_RE = re.compile(
    r'<service\s+android:name="com\.tareq\.motiv_force\.ServiceMonitor"[^>]*/>',
    re.DOTALL,
)

SERVICE_MONITOR_XML_REPLACEMENT = """<service android:name="com.tareq.motiv_force.ServiceMonitor"
             android:process=":service_monitor"
             android:foregroundServiceType="specialUse"
             android:exported="false">
        <property
            android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
            android:value="Monitoring foreground app usage to enforce user-configured screen time limits" />
    </service>"""

CONDBLOCKER_STRINGS_XML = """<?xml version="1.0" encoding="utf-8"?>
<resources>
    <string name="accessibility_service_description">مراقبة التطبيق المفتوح وحماية تطبيق Motiv_Force من الإيقاف أو الحذف أثناء فترة القفل الصارم.</string>
</resources>
"""

_DESC_RE = re.compile(
    r'\n?[ \t]*<string\s+name="accessibility_service_description"[^>]*>.*?</string>',
    re.DOTALL,
)


def _find_dist_dirs(project_dir: str) -> list[str]:
    candidates = []
    cwd = os.getcwd()
    if "dists" in cwd and os.path.isdir(cwd):
        candidates.append(cwd)

    pattern = os.path.join(
        project_dir, ".buildozer", "android", "platform", "build-*", "dists", "*"
    )
    for path in glob.glob(pattern):
        if os.path.isdir(path):
            candidates.append(path)

    return list(dict.fromkeys(candidates))


def _inject_into_dist(dist_dir: str, project_dir: str):
    # 1. نسخ ملفات res/xml
    src_xml = os.path.join(project_dir, "res", "xml")
    dst_xml = os.path.join(dist_dir, "src", "main", "res", "xml")
    if os.path.isdir(src_xml):
        os.makedirs(dst_xml, exist_ok=True)
        for fname in os.listdir(src_xml):
            if fname.endswith(".xml"):
                shutil.copy2(os.path.join(src_xml, fname), os.path.join(dst_xml, fname))

    # 2. إضافة ملف النصوص وتحديث اسم التطبيق
    values_dir = os.path.join(dist_dir, "src", "main", "res", "values")
    os.makedirs(values_dir, exist_ok=True)
    legacy_strings = os.path.join(values_dir, "motivator_strings.xml")
    if os.path.exists(legacy_strings):
        os.remove(legacy_strings)
    with open(os.path.join(values_dir, "motiv_force_strings.xml"), "w", encoding="utf-8") as f:
        f.write(CONDBLOCKER_STRINGS_XML)

    strings_tmpl = os.path.join(dist_dir, "templates", "strings.tmpl.xml")
    if os.path.exists(strings_tmpl):
        with open(strings_tmpl, "r", encoding="utf-8") as f:
            s_content = f.read()
        new_content = _DESC_RE.sub("", s_content).replace(
            '<string name="app_name">{{ args.name }}</string>',
            '<string name="app_name">Motiv_Force</string>',
        )
        if new_content != s_content:
            with open(strings_tmpl, "w", encoding="utf-8") as f:
                f.write(new_content)

    strings_xml = os.path.join(values_dir, "strings.xml")
    if os.path.exists(strings_xml):
        with open(strings_xml, "r", encoding="utf-8") as f:
            x_content = f.read()
        new_x_content = _DESC_RE.sub("", x_content)
        if new_x_content != x_content:
            with open(strings_xml, "w", encoding="utf-8") as f:
                f.write(new_x_content)

    # 3. تعديل قالب AndroidManifest.tmpl.xml وملف AndroidManifest.xml
    tmpl_candidates = [
        os.path.join(dist_dir, "templates", "AndroidManifest.tmpl.xml"),
        os.path.join(dist_dir, "src", "main", "AndroidManifest.xml"),
    ]
    for tmpl_path in tmpl_candidates:
        if os.path.exists(tmpl_path):
            with open(tmpl_path, "r", encoding="utf-8") as f:
                content = f.read()

            updated = content
            if "PROPERTY_SPECIAL_USE_FGS_SUBTYPE" not in updated:
                updated = SERVICE_MONITOR_TMPL_RE.sub(_replace_tmpl_service, updated)
                updated = SERVICE_MONITOR_XML_RE.sub(SERVICE_MONITOR_XML_REPLACEMENT, updated)

            if "AppBlockerAccessibilityService" not in updated and "</application>" in updated:
                updated = updated.replace(
                    "</application>",
                    f"{MANIFEST_INJECTION}\n</application>",
                    1,
                )

            if updated != content:
                with open(tmpl_path, "w", encoding="utf-8") as f:
                    f.write(updated)
                print(f"[P4A_HOOK] Updated Manifest in {tmpl_path}")


def before_apk_build(toolchain):
    project_dir = os.path.dirname(os.path.abspath(__file__))
    for d in _find_dist_dirs(project_dir):
        _inject_into_dist(d, project_dir)


def after_apk_build(toolchain):
    project_dir = os.path.dirname(os.path.abspath(__file__))
    for d in _find_dist_dirs(project_dir):
        _inject_into_dist(d, project_dir)


def before_apk_assemble(toolchain):
    project_dir = os.path.dirname(os.path.abspath(__file__))
    for d in _find_dist_dirs(project_dir):
        _inject_into_dist(d, project_dir)
