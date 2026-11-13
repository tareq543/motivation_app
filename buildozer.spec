[app]

# اسم لاتيني لملف الـ APK لتجنب خطأ الترميز في سطر الأوامر (سيظهر بالعربية على الهاتف)
title = CondBlocker
package.name = condblocker
package.domain = org.guardian

source.dir = .
source.include_exts = py,png,jpg,kv,atlas,xml,java,json
version = 1.0.0

requirements = python3,kivy==2.3.0,kivymd==1.2.0,pyjnius,sqlite3,arabic-reshaper,python-bidi==0.4.2

orientation = portrait
fullscreen = 0

services = monitor:android_services.py:foreground

android.permissions = PACKAGE_USAGE_STATS,SYSTEM_ALERT_WINDOW,FOREGROUND_SERVICE,FOREGROUND_SERVICE_SPECIAL_USE,WAKE_LOCK,RECEIVE_BOOT_COMPLETED,QUERY_ALL_PACKAGES,POST_NOTIFICATIONS

android.api = 34
android.minapi = 26
android.ndk = 25b
android.archs = arm64-v8a
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
