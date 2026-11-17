[app]

title = Motivator
package.name = motivator
package.domain = com.tareq

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,xml,java,json,ttf
version = 1.0.0

requirements = python3,sqlite3,kivy==2.3.0,kivymd==1.2.0,arabic-reshaper,python-bidi==0.4.2,pillow,pyjnius
orientation = portrait
fullscreen = 0

services = monitor:service.py

android.permissions = POST_NOTIFICATIONS, FOREGROUND_SERVICE, FOREGROUND_SERVICE_SPECIAL_USE, PACKAGE_USAGE_STATS, QUERY_ALL_PACKAGES, SYSTEM_ALERT_WINDOW, BIND_ACCESSIBILITY_SERVICE, BIND_DEVICE_ADMIN, RECEIVE_BOOT_COMPLETED, WAKE_LOCK

android.api = 34
android.minapi = 24
android.ndk_api = 24
android.ndk = 25b
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = false
android.accept_sdk_license = True

p4a.branch = v2024.01.21

android.add_src = native_java
p4a.hook = p4a_hook.py

android.entrypoint = org.kivy.android.PythonActivity
android.private_storage = True

[buildozer]
log_level = 2
warn_on_root = 1
