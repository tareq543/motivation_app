"""خدمة المراقبة في الخلفية.

تعمل في عملية (process) منفصلة عن التطبيق، ولا تستورد أي كود واجهة:
لا kivy ولا kivymd ولا main.py. لذلك سطر "Initializing Python" الثاني في
logcat طبيعي ولا يعني وجود نسختين من الواجهة.

إذا ظهر تحذير kivy أدناه فالسبب استيراد جديد لملف واجهة داخل سلسلة
android_services، وهو ما يجب إزالته فوراً (سياق GL غير موجود في الخدمة).
"""
import sys

from android_services import run_monitor_service

if __name__ == "__main__":
    if "kivy" in sys.modules:
        print("[SERVICE] warning: kivy got imported inside the service process")
    run_monitor_service()
