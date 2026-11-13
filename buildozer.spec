[app]

# اسم التطبيق الظاهر على الهاتف
title = الحظر المشروط

# معرف الحزمة (ينتج org.guardian.condblocker)
package.name = condblocker
package.domain = org.guardian

# مجلد المصدر والملفات المضمنة
source.dir = .
source.include_exts = py,png,jpg,kv,atlas,xml,java,json

# رقم الإصدار
version = 1.0.0

# المكتبات والمتطلبات (تم تثبيت python-bidi==0.4.2 لتوافقها الكامل مع أندرويد بدون Rust)
requirements = hostpython3==3.11.6,python3==3.11.6,kivy==2.3.0,kivymd==1.2.0,pyjnius,sqlite3,arabic-reshaper,python-bidi==0.4.2
# اتجاه الشاشة
orientation = portrait
fullscreen = 0

# الخدمة الخلفية المستمرة (Foreground Service)
services = monitor:android_services.py:foreground

# صلاحيات أندرويد المطلوبة
android.permissions = PACKAGE_USAGE_STATS,SYSTEM_ALERT_WINDOW,FOREGROUND_SERVICE,FOREGROUND_SERVICE_SPECIAL_USE,WAKE_LOCK,RECEIVE_BOOT_COMPLETED,QUERY_ALL_PACKAGES,POST_NOTIFICATIONS

# إعدادات Android SDK و NDK
android.api = 34
android.minapi = 26
android.ndk = 25b
android.archs = arm64-v8a,armeabi-v7a
android.allow_backup = false
android.accept_sdk_license = True

# دمج ملفات Java الأصلية
android.add_src = native_java

# معطل عمداً لأن p4a_hook.py ينسخ ملفات res/xml تلقائياً بأمان
# android.add_resources = res

# خطاف حقن AccessibilityService و DeviceAdminReceiver و BootReceiver و Manifest
p4a.hook = p4a_hook.py

# نقطة دخول ملف Python الرئيسي
android.entrypoint = org.kivy.android.PythonActivity

# منع أندرويد من حفظ البيانات تلقائياً في السحابة
android.private_storage = True

[buildozer]

# مستوى السجل في البناء (2 لعرض التفاصيل الكاملة في GitHub Actions)
log_level = 2

# تحذير عند التشغيل بصلاحية root
warn_on_root = 1
