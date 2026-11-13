import os
import threading
import time
from datetime import datetime

from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.image import Image
from kivy.uix.scrollview import ScrollView
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDFlatButton, MDIconButton, MDRaisedButton
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDLabel
from kivymd.uix.pickers import MDTimePicker
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.screen import MDScreen
from kivymd.uix.screenmanager import MDScreenManager
from kivymd.uix.selectioncontrol import MDCheckbox, MDSwitch
from kivymd.uix.snackbar import MDSnackbar
from kivymd.uix.textfield import MDTextField

from database import (
    CondBlockerDB,
    LOCK_STATE_LOCKED,
    LOCK_STATE_UNLOCKED_WINDOW,
    LOCK_STATE_WAITING_24H,
)
from usage_tracker import IS_ANDROID, UsageTracker, get_android_clocks

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    def ar(text: str) -> str:
        if not text:
            return ""
        has_arabic = any("\u0600" <= c <= "\u06FF" for c in text)
        if not has_arabic:
            return text
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
except Exception:
    def ar(text: str) -> str:
        return text


if IS_ANDROID:
    from jnius import autoclass

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Context = autoclass("android.content.Context")
    Intent = autoclass("android.content.Intent")
    Uri = autoclass("android.net.Uri")
    Settings = autoclass("android.provider.Settings")
    AppOpsManager = autoclass("android.app.AppOpsManager")
    Process = autoclass("android.os.Process")
    ComponentName = autoclass("android.content.ComponentName")
    DevicePolicyManager = autoclass("android.app.admin.DevicePolicyManager")
    BuildVersion = autoclass("android.os.Build$VERSION")


ARABIC_FONT = "Roboto"
for candidate in [
    "/system/fonts/NotoNaskhArabic-Regular.ttf",
    "/system/fonts/NotoSansArabic-Regular.ttf",
    "/system/fonts/DroidSansArabic.ttf",
]:
    if os.path.exists(candidate):
        LabelBase.register(name="ArabicUI", fn_regular=candidate, fn_bold=candidate)
        ARABIC_FONT = "ArabicUI"
        break


THEME = {
    "bg_primary": "#0B1120",
    "bg_secondary": "#111827",
    "card": "#1E293B",
    "accent": "#06B6D4",
    "warning": "#F97316",
    "text": "#F8FAFC",
    "muted": "#94A3B8",
    "success": "#10B981",
    "danger": "#EF4444",
}


KV = f"""
<DarkCard@MDCard>:
    md_bg_color: "{THEME['card']}"
    radius: [dp(16)]
    padding: dp(14)
    spacing: dp(10)
    elevation: 2
    size_hint_y: None
    height: self.minimum_height

<OnboardingScreen>:
    md_bg_color: "{THEME['bg_primary']}"
    MDBoxLayout:
        orientation: "vertical"
        padding: dp(18)
        spacing: dp(14)

        MDLabel:
            id: lbl_title
            text: ""
            font_name: "{ARABIC_FONT}"
            font_size: "22sp"
            bold: True
            theme_text_color: "Custom"
            text_color: "{THEME['text']}"
            halign: "center"
            size_hint_y: None
            height: dp(38)

        MDLabel:
            id: lbl_sub
            text: ""
            font_name: "{ARABIC_FONT}"
            font_size: "13sp"
            theme_text_color: "Custom"
            text_color: "{THEME['muted']}"
            halign: "center"
            size_hint_y: None
            height: dp(46)

        ScrollView:
            MDBoxLayout:
                id: perm_list
                orientation: "vertical"
                spacing: dp(12)
                size_hint_y: None
                height: self.minimum_height

        MDRaisedButton:
            id: btn_continue
            text: ""
            font_name: "{ARABIC_FONT}"
            md_bg_color: "{THEME['accent']}"
            text_color: "{THEME['bg_primary']}"
            size_hint_x: 1
            height: dp(48)
            disabled: True
            on_release: root.enter_dashboard()

<DashboardScreen>:
    md_bg_color: "{THEME['bg_primary']}"
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(60)
            md_bg_color: "{THEME['bg_secondary']}"
            padding: [dp(16), dp(8)]
            spacing: dp(10)

            MDIconButton:
                icon: "cog"
                theme_icon_color: "Custom"
                icon_color: "{THEME['accent']}"
                on_release: app.go_screen("settings")

            MDLabel:
                id: dash_header
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "19sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: "{THEME['text']}"

        ScrollView:
            MDBoxLayout:
                orientation: "vertical"
                padding: dp(14)
                spacing: dp(14)
                size_hint_y: None
                height: self.minimum_height

                DarkCard:
                    id: lock_card
                    orientation: "vertical"
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(30)
                        MDLabel:
                            id: lock_badge
                            text: ""
                            font_name: "{ARABIC_FONT}"
                            bold: True
                            font_size: "15sp"
                            halign: "right"
                            theme_text_color: "Custom"
                            text_color: "{THEME['warning']}"
                    MDLabel:
                        id: lock_desc
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)
                    MDRaisedButton:
                        id: btn_lock_action
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['warning']}"
                        text_color: "{THEME['bg_primary']}"
                        size_hint_x: 1
                        on_release: root.on_lock_action()

                MDBoxLayout:
                    size_hint_y: None
                    height: dp(40)
                    MDRaisedButton:
                        id: btn_add_rule
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['accent']}"
                        text_color: "{THEME['bg_primary']}"
                        on_release: root.open_new_rule()
                    MDLabel:
                        id: rules_section_lbl
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "16sp"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['text']}"

                MDBoxLayout:
                    id: rules_container
                    orientation: "vertical"
                    spacing: dp(12)
                    size_hint_y: None
                    height: self.minimum_height

<RuleBuilderScreen>:
    md_bg_color: "{THEME['bg_primary']}"
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(58)
            md_bg_color: "{THEME['bg_secondary']}"
            padding: [dp(12), dp(8)]
            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: "{THEME['text']}"
                on_release: app.go_screen("dashboard")
            MDLabel:
                id: rb_title
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "18sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: "{THEME['text']}"

        ScrollView:
            MDBoxLayout:
                orientation: "vertical"
                padding: dp(14)
                spacing: dp(14)
                size_hint_y: None
                height: self.minimum_height

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_rule_name
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['text']}"
                        size_hint_y: None
                        height: dp(24)
                    MDTextField:
                        id: input_rule_name
                        font_name: "{ARABIC_FONT}"
                        mode: "rectangle"
                        line_color_normal: "{THEME['muted']}"
                        line_color_focus: "{THEME['accent']}"
                        text_color_normal: "{THEME['text']}"
                        text_color_focus: "{THEME['text']}"

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_target_sec
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['warning']}"
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_target_summary
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)
                    MDRaisedButton:
                        id: btn_pick_targets
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['warning']}"
                        text_color: "{THEME['bg_primary']}"
                        size_hint_x: 1
                        on_release: root.open_app_picker("target")

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_source_sec
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['accent']}"
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_source_summary
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)
                    MDRaisedButton:
                        id: btn_pick_sources
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['accent']}"
                        text_color: "{THEME['bg_primary']}"
                        size_hint_x: 1
                        on_release: root.open_app_picker("source")

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_minutes
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['text']}"
                        size_hint_y: None
                        height: dp(26)
                    MDTextField:
                        id: input_minutes
                        input_filter: "int"
                        text: "20"
                        mode: "rectangle"
                        line_color_normal: "{THEME['muted']}"
                        line_color_focus: "{THEME['accent']}"
                        text_color_normal: "{THEME['text']}"
                        text_color_focus: "{THEME['text']}"

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(44)
                        spacing: dp(10)
                        MDSwitch:
                            id: switch_mode
                            active: False
                            thumb_color_active: "{THEME['accent']}"
                            on_active: root.on_mode_switch(self.active)
                        MDLabel:
                            id: lbl_mode_title
                            text: ""
                            font_name: "{ARABIC_FONT}"
                            halign: "right"
                            theme_text_color: "Custom"
                            text_color: "{THEME['text']}"

                    MDLabel:
                        id: lbl_mode_desc
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)

                MDRaisedButton:
                    id: btn_save_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    md_bg_color: "{THEME['accent']}"
                    text_color: "{THEME['bg_primary']}"
                    size_hint_x: 1
                    height: dp(48)
                    on_release: root.save_rule()

                MDRaisedButton:
                    id: btn_delete_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    md_bg_color: "{THEME['danger']}"
                    text_color: "{THEME['text']}"
                    size_hint_x: 1
                    height: dp(44)
                    opacity: 0
                    disabled: True
                    on_release: root.delete_current_rule()

<SettingsScreen>:
    md_bg_color: "{THEME['bg_primary']}"
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(58)
            md_bg_color: "{THEME['bg_secondary']}"
            padding: [dp(12), dp(8)]
            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: "{THEME['text']}"
                on_release: app.go_screen("dashboard")
            MDLabel:
                id: settings_title
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "18sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: "{THEME['text']}"

        ScrollView:
            MDBoxLayout:
                orientation: "vertical"
                padding: dp(14)
                spacing: dp(14)
                size_hint_y: None
                height: self.minimum_height

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_reset_title
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['accent']}"
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_reset_val
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "14sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['text']}"
                        size_hint_y: None
                        height: dp(28)
                    MDRaisedButton:
                        id: btn_pick_reset_time
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['accent']}"
                        text_color: "{THEME['bg_primary']}"
                        size_hint_x: 1
                        on_release: root.open_time_picker()

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_strict_title
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['warning']}"
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_strict_status
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                    MDRaisedButton:
                        id: btn_strict_unlock
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['warning']}"
                        text_color: "{THEME['bg_primary']}"
                        size_hint_x: 1
                        on_release: root.handle_strict_button()

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_perms_link
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['text']}"
                        size_hint_y: None
                        height: dp(26)
                    MDRaisedButton:
                        id: btn_recheck_perms
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: "{THEME['bg_secondary']}"
                        text_color: "{THEME['accent']}"
                        size_hint_x: 1
                        on_release: app.go_screen("onboarding")

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_tamper_title
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['danger']}"
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_tamper_logs
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: "{THEME['muted']}"
                        size_hint_y: None
                        height: self.texture_size[1] + dp(8)
"""


def format_hhmmss(seconds: float) -> str:
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def notify_snack(msg: str):
    sb = MDSnackbar(
        MDLabel(
            text=ar(msg),
            font_name=ARABIC_FONT,
            theme_text_color="Custom",
            text_color=THEME["text"],
            halign="right",
        ),
        md_bg_color=THEME["card"],
        duration=2.5,
    )
    sb.open()


class PermissionBridge:
    """فحص وتوجيه المستخدم لتفعيل الصلاحيات الأربع الأساسية."""

    @staticmethod
    def _activity():
        if IS_ANDROID:
            return PythonActivity.mActivity
        return None

    @staticmethod
    def has_usage_access() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            app_ops = act.getSystemService(Context.APP_OPS_SERVICE)
            mode = app_ops.checkOpNoThrow(
                AppOpsManager.OPSTR_GET_USAGE_STATS,
                Process.myUid(),
                act.getPackageName(),
            )
            return mode == AppOpsManager.MODE_ALLOWED
        except Exception:
            return False

    @staticmethod
    def has_overlay_permission() -> bool:
        if not IS_ANDROID:
            return True
        try:
            return bool(Settings.canDrawOverlays(PythonActivity.mActivity))
        except Exception:
            return False

    @staticmethod
    def has_accessibility_enabled() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            my_pkg = str(act.getPackageName())
            expected = f"{my_pkg}/org.guardian.condblocker.AppBlockerAccessibilityService"
            enabled = Settings.Secure.getInt(
                act.getContentResolver(), Settings.Secure.ACCESSIBILITY_ENABLED, 0
            )
            if enabled != 1:
                return False
            services = Settings.Secure.getString(
                act.getContentResolver(), Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            )
            if not services:
                return False
            s_str = str(services)
            return expected in s_str or f"{my_pkg}/.AppBlockerAccessibilityService" in s_str
        except Exception:
            return False

    @staticmethod
    def has_device_admin() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            dpm = act.getSystemService(Context.DEVICE_POLICY_SERVICE)
            comp = ComponentName(
                act.getPackageName(),
                "org.guardian.condblocker.AppDeviceAdminReceiver",
            )
            return bool(dpm.isAdminActive(comp))
        except Exception:
            return False

    @staticmethod
    def all_granted() -> bool:
        return (
            PermissionBridge.has_usage_access()
            and PermissionBridge.has_overlay_permission()
            and PermissionBridge.has_accessibility_enabled()
            and PermissionBridge.has_device_admin()
        )

    @staticmethod
    def open_usage_settings():
        if not IS_ANDROID:
            return
        act = PythonActivity.mActivity
        intent = Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        act.startActivity(intent)

    @staticmethod
    def open_overlay_settings():
        if not IS_ANDROID:
            return
        act = PythonActivity.mActivity
        intent = Intent(
            Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
            Uri.parse(f"package:{act.getPackageName()}"),
        )
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        act.startActivity(intent)

    @staticmethod
    def open_accessibility_settings():
        if not IS_ANDROID:
            return
        act = PythonActivity.mActivity
        intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        act.startActivity(intent)

    @staticmethod
    def request_device_admin():
        if not IS_ANDROID:
            return
        act = PythonActivity.mActivity
        comp = ComponentName(
            act.getPackageName(),
            "org.guardian.condblocker.AppDeviceAdminReceiver",
        )
        intent = Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
        intent.putExtra(DevicePolicyManager.EXTRA_DEVICE_ADMIN, comp)
        intent.putExtra(
            DevicePolicyManager.EXTRA_ADD_EXPLANATION,
            "مطلوب لمنع حذف التطبيق أو تعطيله خارج نافذة الـ 30 دقيقة المسموحة.",
        )
        act.startActivityForResult(intent, 1001)


class OnboardingScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.ids.lbl_title.text = ar("تهيئة الصلاحيات الأساسية")
        self.ids.lbl_sub.text = ar(
            "لضمان عمل الحظر المشروط والحماية الصارمة دون توقف، يجب تفعيل الصلاحيات الأربع التالية:"
        )
        self.ids.btn_continue.text = ar("متابعة إلى لوحة التحكم")
        self.refresh_permissions()
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_permissions(), 1.5)

    def on_pre_leave(self, *args):
        if hasattr(self, "_ev") and self._ev:
            self._ev.cancel()

    def refresh_permissions(self):
        container = self.ids.perm_list
        container.clear_widgets()

        items = [
            (
                "الوصول لبيانات الاستخدام (Usage Access)",
                "لقراءة دقائق استخدام التطبيقات بدقة يومياً.",
                PermissionBridge.has_usage_access(),
                PermissionBridge.open_usage_settings,
            ),
            (
                "الظهور فوق التطبيقات (Display Over Other Apps)",
                "لعرض شاشة الحظر الفورية عند فتح تطبيق مقيد.",
                PermissionBridge.has_overlay_permission(),
                PermissionBridge.open_overlay_settings,
            ),
            (
                "خدمة إمكانية الوصول (Accessibility Service)",
                "للمراقبة الفورية ومنع الإيقاف الإجباري أو سحب الصلاحيات.",
                PermissionBridge.has_accessibility_enabled(),
                PermissionBridge.open_accessibility_settings,
            ),
            (
                "مشرف الجهاز (Device Admin)",
                "لمنع إلغاء تثبيت التطبيق أثناء حالة القفل الصارم.",
                PermissionBridge.has_device_admin(),
                PermissionBridge.request_device_admin,
            ),
        ]

        all_ok = True
        for title, desc, granted, action in items:
            if not granted:
                all_ok = False
            card = MDCard(
                md_bg_color=THEME["card"],
                radius=[dp(14)],
                padding=dp(14),
                spacing=dp(8),
                orientation="vertical",
                size_hint_y=None,
                height=dp(132),
            )
            top = MDBoxLayout(size_hint_y=None, height=dp(28))
            status_lbl = MDLabel(
                text=ar("مفعلة ✓" if granted else "غير مفعلة ✗"),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="13sp",
                theme_text_color="Custom",
                text_color=THEME["success"] if granted else THEME["warning"],
                size_hint_x=0.35,
            )
            title_lbl = MDLabel(
                text=ar(title),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="14sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["text"],
                size_hint_x=0.65,
            )
            top.add_widget(status_lbl)
            top.add_widget(title_lbl)

            desc_lbl = MDLabel(
                text=ar(desc),
                font_name=ARABIC_FONT,
                font_size="12sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["muted"],
                size_hint_y=None,
                height=dp(34),
            )
            btn = MDRaisedButton(
                text=ar("مفعلة بالفعل" if granted else "تفعيل الصلاحية الآن"),
                font_name=ARABIC_FONT,
                md_bg_color=THEME["bg_secondary"] if granted else THEME["accent"],
                text_color=THEME["success"] if granted else THEME["bg_primary"],
                disabled=granted,
                size_hint_x=1,
                on_release=lambda x, cb=action: cb(),
            )
            card.add_widget(top)
            card.add_widget(desc_lbl)
            card.add_widget(btn)
            container.add_widget(card)

        self.ids.btn_continue.disabled = not all_ok
        if all_ok:
            app = MDApp.get_running_app()
            app.db.set_onboarding_complete(True)
            app.sync_service_state()

    def enter_dashboard(self):
        app = MDApp.get_running_app()
        app.db.set_onboarding_complete(True)
        app.start_background_service()
        app.go_screen("dashboard")


class DashboardScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.ids.dash_header.text = ar("لوحة التحكم - الحظر المشروط")
        self.ids.btn_add_rule.text = ar("+ قاعدة جديدة")
        self.ids.rules_section_lbl.text = ar("القواعد النشطة")
        self.refresh_dashboard()
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_dashboard(), 2.0)

    def on_pre_leave(self, *args):
        if hasattr(self, "_ev") and self._ev:
            self._ev.cancel()

    def refresh_dashboard(self):
        app = MDApp.get_running_app()
        now_elapsed, now_wall = get_android_clocks()
        lock_info = app.db.sync_monotonic_clock(now_elapsed, now_wall)
        state = lock_info["state"]
        rem = lock_info["remaining_seconds"]

        if state == LOCK_STATE_LOCKED:
            self.ids.lock_badge.text = ar("حالة الحماية: مقفل بصرامة (LOCKED)")
            self.ids.lock_badge.text_color = THEME["warning"]
            self.ids.lock_desc.text = ar(
                "التعديل والحذف وإلغاء التثبيت محظورة تماماً. لفتح نافذة تعديل (30 دقيقة) يجب بدء مؤقت الانتظار الإلزامي 24 ساعة."
            )
            self.ids.btn_lock_action.text = ar("طلب إلغاء القفل (بدء عداد 24 ساعة)")
            self.ids.btn_lock_action.md_bg_color = THEME["warning"]
        elif state == LOCK_STATE_WAITING_24H:
            self.ids.lock_badge.text = ar(f"عد تنازلي لفك القفل: {format_hhmmss(rem)}")
            self.ids.lock_badge.text_color = THEME["accent"]
            self.ids.lock_desc.text = ar(
                "تظل جميع القيود مفعّلة حتى انتهاء الـ 24 ساعة، بعدها تفتح نافذة تعديل لمدة 30 دقيقة فقط."
            )
            self.ids.btn_lock_action.text = ar("إلغاء طلب فك القفل والعودة للقفل الفوري")
            self.ids.btn_lock_action.md_bg_color = THEME["danger"]
        else:
            self.ids.lock_badge.text = ar(f"نافذة التعديل مفتوحة: {format_hhmmss(rem)}")
            self.ids.lock_badge.text_color = THEME["success"]
            self.ids.lock_desc.text = ar(
                "يمكنك الآن تعديل أو حذف القواعد وتغيير وقت التصفير أو إلغاء التثبيت قبل انتهاء المهلة."
            )
            self.ids.btn_lock_action.text = ar("إعادة القفل الصارم الآن")
            self.ids.btn_lock_action.md_bg_color = THEME["accent"]

        can_mod = app.db.can_modify_restrictions()
        self.ids.btn_add_rule.disabled = not can_mod

        evaluations, blocked_targets, _ = app.tracker.evaluate_all_rules(app.db)
        app.db.export_blocker_state(app.state_json_path, list(blocked_targets.keys()))

        container = self.ids.rules_container
        container.clear_widgets()

        if not evaluations:
            empty_card = MDCard(
                md_bg_color=THEME["card"],
                radius=[dp(16)],
                padding=dp(18),
                size_hint_y=None,
                height=dp(96),
            )
            empty_card.add_widget(
                MDLabel(
                    text=ar("لا توجد قواعد بعد. اضغط على (+ قاعدة جديدة) لإنشاء أول قاعدة حظر مشروط."),
                    font_name=ARABIC_FONT,
                    halign="center",
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                )
            )
            container.add_widget(empty_card)
            return

        for ev in evaluations:
            rule = ev["rule"]
            is_unlocked = ev["is_unlocked"]
            prog = ev["overall_progress"]
            mode_ar = "تجميعي (SUM)" if rule["mode"] == "SUM" else "فردي إلزامي (INDIVIDUAL)"

            card = MDCard(
                md_bg_color=THEME["card"],
                radius=[dp(16)],
                padding=dp(14),
                spacing=dp(8),
                orientation="vertical",
                size_hint_y=None,
                height=dp(188),
            )

            top_row = MDBoxLayout(size_hint_y=None, height=dp(30))
            status_lbl = MDLabel(
                text=ar("مفتوح ✓" if is_unlocked else "محظور 🔒"),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="13sp",
                theme_text_color="Custom",
                text_color=THEME["success"] if is_unlocked else THEME["warning"],
                size_hint_x=0.32,
            )
            name_lbl = MDLabel(
                text=ar(rule["name"]),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="16sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["text"],
                size_hint_x=0.68,
            )
            top_row.add_widget(status_lbl)
            top_row.add_widget(name_lbl)

            tgt_names = ", ".join(app.tracker.get_app_label(p) for p in rule["target_packages"][:3])
            src_names = ", ".join(app.tracker.get_app_label(p) for p in rule["source_packages"][:3])
            meta_lbl = MDLabel(
                text=ar(
                    f"المحظور: {tgt_names}\nالمصدر: {src_names} | النمط: {mode_ar}"
                ),
                font_name=ARABIC_FONT,
                font_size="12sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["muted"],
                size_hint_y=None,
                height=dp(38),
            )

            prog_lbl = MDLabel(
                text=ar(
                    f"المنجز: {ev['completed_minutes']:.1f} د | المتبقي: {ev['remaining_minutes']:.1f} د (الهدف: {ev['required_minutes']} د)"
                ),
                font_name=ARABIC_FONT,
                font_size="12sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["accent"],
                size_hint_y=None,
                height=dp(22),
            )

            pbar = MDProgressBar(
                value=int(prog * 100),
                color=THEME["success"] if is_unlocked else THEME["accent"],
                back_color=THEME["bg_primary"],
                size_hint_y=None,
                height=dp(8),
            )

            actions_row = MDBoxLayout(size_hint_y=None, height=dp(38), spacing=dp(10))
            edit_btn = MDRaisedButton(
                text=ar("تعديل القاعدة" if can_mod else "مقفل 🔒"),
                font_name=ARABIC_FONT,
                md_bg_color=THEME["bg_secondary"],
                text_color=THEME["accent"] if can_mod else THEME["muted"],
                disabled=not can_mod,
                on_release=lambda x, rid=rule["id"]: self.open_edit_rule(rid),
            )
            sw = MDSwitch(
                active=rule["enabled"],
                disabled=not can_mod,
                thumb_color_active=THEME["accent"],
                on_active=lambda inst, val, rid=rule["id"]: self.on_toggle_rule(rid, val),
            )
            actions_row.add_widget(edit_btn)
            actions_row.add_widget(sw)

            card.add_widget(top_row)
            card.add_widget(meta_lbl)
            card.add_widget(prog_lbl)
            card.add_widget(pbar)
            card.add_widget(actions_row)
            container.add_widget(card)

    def on_lock_action(self):
        app = MDApp.get_running_app()
        now_elapsed, now_wall = get_android_clocks()
        state = app.db.get_lock_state()["state"]
        if state == LOCK_STATE_LOCKED:
            app.db.request_unlock_24h(now_elapsed, now_wall)
            notify_snack("بدأ العداد التنازلي الإلزامي لمدة 24 ساعة.")
        else:
            app.db.cancel_unlock_request()
            notify_snack("تم تفعيل القفل الصارم فوراً.")
        app.sync_service_state()
        self.refresh_dashboard()

    def on_toggle_rule(self, rule_id: int, val: bool):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("لا يمكن تعديل القواعد أثناء حالة القفل الصارم.")
            self.refresh_dashboard()
            return
        app.db.toggle_rule(rule_id, val)
        app.sync_service_state()

    def open_new_rule(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("التعديل متاح فقط خلال نافذة الـ 30 دقيقة.")
            return
        rb = app.root.get_screen("rule_builder")
        rb.load_rule(None)
        app.go_screen("rule_builder")

    def open_edit_rule(self, rule_id: int):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("التعديل متاح فقط خلال نافذة الـ 30 دقيقة.")
            return
        rb = app.root.get_screen("rule_builder")
        rb.load_rule(rule_id)
        app.go_screen("rule_builder")


class RuleBuilderScreen(MDScreen):
    editing_rule_id = None
    selected_targets: list[str] = []
    selected_sources: list[str] = []
    picker_dialog = None
    _installed_apps: list[dict] = []

    def on_pre_enter(self, *args):
        self.ids.lbl_rule_name.text = ar("اسم القاعدة")
        self.ids.lbl_target_sec.text = ar("التطبيقات المحظورة (Target Apps)")
        self.ids.btn_pick_targets.text = ar("اختيار التطبيقات المحظورة")
        self.ids.lbl_source_sec.text = ar("تطبيقات الشرط / المصدر (Source Apps)")
        self.ids.btn_pick_sources.text = ar("اختيار تطبيقات المصدر")
        self.ids.lbl_minutes.text = ar("الوقت المطلوب بالدقائق (بالضبط)")
        self.ids.btn_save_rule.text = ar("حفظ وتفعيل القاعدة")
        self.ids.btn_delete_rule.text = ar("حذف هذه القاعدة")
        self._update_mode_texts(self.ids.switch_mode.active)
        self._preload_apps()

    def _preload_apps(self):
        app = MDApp.get_running_app()
        def _worker():
            self._installed_apps = app.tracker.get_installed_apps()
        threading.Thread(target=_worker, daemon=True).start()

    def load_rule(self, rule_id: int | None):
        app = MDApp.get_running_app()
        self.editing_rule_id = rule_id
        if rule_id is None:
            self.ids.rb_title.text = ar("إنشاء قاعدة حظر جديدة")
            self.ids.input_rule_name.text = ""
            self.selected_targets = []
            self.selected_sources = []
            self.ids.input_minutes.text = "20"
            self.ids.switch_mode.active = False
            self.ids.btn_delete_rule.opacity = 0
            self.ids.btn_delete_rule.disabled = True
        else:
            rule = app.db.get_rule(rule_id)
            if not rule:
                return
            self.ids.rb_title.text = ar("تعديل قاعدة الحظر")
            self.ids.input_rule_name.text = rule["name"]
            self.selected_targets = list(rule["target_packages"])
            self.selected_sources = list(rule["source_packages"])
            self.ids.input_minutes.text = str(rule["required_minutes"])
            self.ids.switch_mode.active = rule["mode"] == "INDIVIDUAL"
            self.ids.btn_delete_rule.opacity = 1
            self.ids.btn_delete_rule.disabled = False

        self._refresh_summaries()
        self._update_mode_texts(self.ids.switch_mode.active)

    def _refresh_summaries(self):
        app = MDApp.get_running_app()
        if self.selected_targets:
            t_txt = ", ".join(app.tracker.get_app_label(p) for p in self.selected_targets)
            self.ids.lbl_target_summary.text = ar(f"المختار ({len(self.selected_targets)}): {t_txt}")
        else:
            self.ids.lbl_target_summary.text = ar("لم يتم اختيار أي تطبيق محظور بعد.")

        if self.selected_sources:
            s_txt = ", ".join(app.tracker.get_app_label(p) for p in self.selected_sources)
            self.ids.lbl_source_summary.text = ar(f"المختار ({len(self.selected_sources)}): {s_txt}")
        else:
            self.ids.lbl_source_summary.text = ar("لم يتم اختيار أي تطبيق مصدر بعد.")

    def on_mode_switch(self, active: bool):
        self._update_mode_texts(active)

    def _update_mode_texts(self, is_individual: bool):
        if is_individual:
            self.ids.lbl_mode_title.text = ar("النمط الفردي الإلزامي (INDIVIDUAL / AND)")
            self.ids.lbl_mode_desc.text = ar(
                "تُفتح التطبيقات المحظورة فقط بعد إتمام الدقائق المطلوبة في كل تطبيق مصدر على حدة."
            )
        else:
            self.ids.lbl_mode_title.text = ar("النمط التجميعي (SUM / OR)")
            self.ids.lbl_mode_desc.text = ar(
                "تُفتح التطبيقات المحظورة بمجرد وصول مجموع دقائق تطبيقات المصدر مجتمعة إلى الرقم المحدد."
            )

    def open_app_picker(self, role: str):
        app = MDApp.get_running_app()
        if not self._installed_apps:
            self._installed_apps = app.tracker.get_installed_apps(force_refresh=True)

        current_set = set(self.selected_targets if role == "target" else self.selected_sources)
        temp_selected = set(current_set)

        content = MDBoxLayout(
            orientation="vertical",
            spacing=dp(8),
            size_hint_y=None,
            height=dp(420),
        )
        search_field = MDTextField(
            hint_text=ar("بحث بالاسم أو الحزمة..."),
            font_name=ARABIC_FONT,
            mode="rectangle",
            size_hint_y=None,
            height=dp(48),
        )
        scroll = ScrollView()
        list_box = MDBoxLayout(orientation="vertical", spacing=dp(6), size_hint_y=None)
        list_box.bind(minimum_height=list_box.setter("height"))
        scroll.add_widget(list_box)
        content.add_widget(search_field)
        content.add_widget(scroll)

        def render_list(query: str = ""):
            list_box.clear_widgets()
            q = query.strip().lower()
            for item in self._installed_apps:
                if q and q not in item["name"].lower() and q not in item["package_name"].lower():
                    continue
                row = MDBoxLayout(size_hint_y=None, height=dp(54), spacing=dp(8), padding=[dp(4), 0])

                cb = MDCheckbox(
                    active=item["package_name"] in temp_selected,
                    size_hint=(None, None),
                    size=(dp(38), dp(38)),
                )
                cb.bind(
                    active=lambda inst, val, p=item["package_name"]: (
                        temp_selected.add(p) if val else temp_selected.discard(p)
                    )
                )

                lbl_col = MDBoxLayout(orientation="vertical")
                lbl_name = MDLabel(
                    text=ar(item["name"]),
                    font_name=ARABIC_FONT,
                    font_size="14sp",
                    bold=True,
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["text"],
                )
                lbl_pkg = MDLabel(
                    text=item["package_name"],
                    font_size="11sp",
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                )
                lbl_col.add_widget(lbl_name)
                lbl_col.add_widget(lbl_pkg)

                row.add_widget(cb)
                row.add_widget(lbl_col)
                if item.get("icon_path") and os.path.exists(item["icon_path"]):
                    row.add_widget(
                        Image(
                            source=item["icon_path"],
                            size_hint=(None, None),
                            size=(dp(36), dp(36)),
                            pos_hint={"center_y": 0.5},
                        )
                    )
                list_box.add_widget(row)

        search_field.bind(text=lambda inst, txt: render_list(txt))
        render_list("")

        def on_confirm(*args):
            if role == "target":
                self.selected_targets = sorted(temp_selected)
            else:
                self.selected_sources = sorted(temp_selected)
            self._refresh_summaries()
            self.picker_dialog.dismiss()

        self.picker_dialog = MDDialog(
            title=ar("اختر التطبيقات"),
            type="custom",
            md_bg_color=THEME["card"],
            content_cls=content,
            buttons=[
                MDFlatButton(
                    text=ar("إلغاء"),
                    font_name=ARABIC_FONT,
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                    on_release=lambda x: self.picker_dialog.dismiss(),
                ),
                MDRaisedButton(
                    text=ar("تأكيد الاختيار"),
                    font_name=ARABIC_FONT,
                    md_bg_color=THEME["accent"],
                    text_color=THEME["bg_primary"],
                    on_release=on_confirm,
                ),
            ],
        )
        self.picker_dialog.open()

    def save_rule(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("التعديل محظور خارج نافذة الـ 30 دقيقة.")
            return

        name = self.ids.input_rule_name.text.strip()
        if not name:
            notify_snack("يرجى كتابة اسم للقاعدة.")
            return
        if not self.selected_targets:
            notify_snack("يرجى اختيار تطبيق واحد محظور على الأقل.")
            return
        if not self.selected_sources:
            notify_snack("يرجى اختيار تطبيق مصدر واحد على الأقل.")
            return
        overlap = set(self.selected_targets) & set(self.selected_sources)
        if overlap:
            notify_snack("لا يمكن أن يكون نفس التطبيق مصدراً ومحظوراً في آنٍ واحد.")
            return

        try:
            minutes = int(self.ids.input_minutes.text.strip())
            if minutes < 1:
                raise ValueError()
        except Exception:
            notify_snack("يرجى إدخال عدد دقائق صحيح (1 أو أكثر).")
            return

        mode = "INDIVIDUAL" if self.ids.switch_mode.active else "SUM"

        if self.editing_rule_id is None:
            app.db.create_rule(name, self.selected_targets, self.selected_sources, minutes, mode, True)
            notify_snack("تم حفظ القاعدة وتفعيل الحماية الصارمة.")
        else:
            app.db.update_rule(
                self.editing_rule_id, name, self.selected_targets, self.selected_sources, minutes, mode, True
            )
            notify_snack("تم تحديث القاعدة بنجاح.")

        app.sync_service_state()
        app.go_screen("dashboard")

    def delete_current_rule(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("الحذف محظور خارج نافذة الـ 30 دقيقة.")
            return
        if self.editing_rule_id is not None:
            app.db.delete_rule(self.editing_rule_id)
            app.sync_service_state()
            notify_snack("تم حذف القاعدة.")
            app.go_screen("dashboard")


class SettingsScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.ids.settings_title.text = ar("الإعدادات والتصفير اليومي")
        self.ids.lbl_reset_title.text = ar("وقت التصفير اليومي المخصص (Daily Reset)")
        self.ids.btn_pick_reset_time.text = ar("تغيير وقت التصفير اليومي")
        self.ids.lbl_strict_title.text = ar("إدارة الحماية الصارمة (24 ساعة -> 30 دقيقة)")
        self.ids.lbl_perms_link.text = ar("فحص صلاحيات النظام")
        self.ids.btn_recheck_perms.text = ar("مراجعة شاشة الصلاحيات")
        self.ids.lbl_tamper_title.text = ar("سجل محاولات التلاعب بالوقت")
        self.refresh_settings()
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_settings(), 2.0)

    def on_pre_leave(self, *args):
        if hasattr(self, "_ev") and self._ev:
            self._ev.cancel()

    def refresh_settings(self):
        app = MDApp.get_running_app()
        h, m = app.db.get_reset_time()
        self.ids.lbl_reset_val.text = ar(f"يتم تصفير عدادات الاستخدام وإعادة القفل يومياً الساعة {h:02d}:{m:02d}")
        can_mod = app.db.can_modify_restrictions()
        self.ids.btn_pick_reset_time.disabled = not can_mod

        now_elapsed, now_wall = get_android_clocks()
        lock_info = app.db.sync_monotonic_clock(now_elapsed, now_wall)
        state = lock_info["state"]
        rem = lock_info["remaining_seconds"]

        if state == LOCK_STATE_LOCKED:
            self.ids.lbl_strict_status.text = ar(
                "النظام مقفل حالياً. لفتح نافذة الـ 30 دقيقة المسموح فيها بالتعديل أو إلغاء مشرف الجهاز وحذف التطبيق، ابدأ عداد الـ 24 ساعة."
            )
            self.ids.btn_strict_unlock.text = ar("بدء عداد الـ 24 ساعة لفك القفل")
        elif state == LOCK_STATE_WAITING_24H:
            self.ids.lbl_strict_status.text = ar(f"جاري العد التنازلي: متبقي {format_hhmmss(rem)}")
            self.ids.btn_strict_unlock.text = ar("إلغاء طلب فك القفل")
        else:
            self.ids.lbl_strict_status.text = ar(f"نافذة التعديل والحذف مفتوحة الآن! متبقي {format_hhmmss(rem)}")
            self.ids.btn_strict_unlock.text = ar("إغلاق النافذة وإعادة القفل فوراً")

        logs = app.db.get_tamper_logs(limit=6)
        if not logs:
            self.ids.lbl_tamper_logs.text = ar("لا توجد محاولات تلاعب مسجلة.")
        else:
            lines = []
            for lg in logs:
                dt_str = datetime.fromtimestamp(lg["timestamp_ms"] / 1000.0).strftime("%Y-%m-%d %H:%M")
                lines.append(f"[{dt_str}] {ar(lg['details'])}")
            self.ids.lbl_tamper_logs.text = "\n".join(lines)

    def open_time_picker(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("تغيير وقت التصفير متاح فقط في نافذة الـ 30 دقيقة.")
            return
        h, m = app.db.get_reset_time()
        picker = MDTimePicker()
        picker.set_time(datetime.now().replace(hour=h, minute=m, second=0, microsecond=0).time())
        picker.bind(on_save=self.on_time_saved)
        picker.open()

    def on_time_saved(self, instance, time_obj):
        app = MDApp.get_running_app()
        app.db.set_reset_time(time_obj.hour, time_obj.minute)
        app.sync_service_state()
        notify_snack(f"تم ضبط وقت التصفير اليومي على {time_obj.hour:02d}:{time_obj.minute:02d}")
        self.refresh_settings()

    def handle_strict_button(self):
        app = MDApp.get_running_app()
        now_elapsed, now_wall = get_android_clocks()
        state = app.db.get_lock_state()["state"]
        if state == LOCK_STATE_LOCKED:
            app.db.request_unlock_24h(now_elapsed, now_wall)
            notify_snack("بدأ عداد الانتظار الإلزامي لمدة 24 ساعة.")
        else:
            app.db.cancel_unlock_request()
            notify_snack("تم إعادة تفعيل القفل الصارم.")
        app.sync_service_state()
        self.refresh_settings()


class CondBlockerApp(MDApp):
    db: CondBlockerDB = None
    tracker: UsageTracker = None
    state_json_path: str = ""

    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Cyan"

        if IS_ANDROID:
            files_dir = str(PythonActivity.mActivity.getFilesDir().getAbsolutePath())
        else:
            files_dir = os.path.join(os.path.expanduser("~"), ".condblocker")

        os.makedirs(files_dir, exist_ok=True)
        self.db = CondBlockerDB(os.path.join(files_dir, "condblocker.db"))
        self.state_json_path = os.path.join(files_dir, "blocker_state.json")
        self.tracker = UsageTracker(cache_dir=files_dir)

        Builder.load_string(KV)
        sm = MDScreenManager()
        sm.add_widget(OnboardingScreen(name="onboarding"))
        sm.add_widget(DashboardScreen(name="dashboard"))
        sm.add_widget(RuleBuilderScreen(name="rule_builder"))
        sm.add_widget(SettingsScreen(name="settings"))

        if PermissionBridge.all_granted() and self.db.is_onboarding_complete():
            sm.current = "dashboard"
            Clock.schedule_once(lambda dt: self.start_background_service(), 0.5)
        else:
            sm.current = "onboarding"

        self.sync_service_state()
        return sm

    def go_screen(self, name: str):
        self.root.current = name

    def sync_service_state(self):
        try:
            _, blocked_targets, _ = self.tracker.evaluate_all_rules(self.db)
            self.db.export_blocker_state(self.state_json_path, list(blocked_targets.keys()))
        except Exception:
            pass

    def start_background_service(self):
        if not IS_ANDROID:
            return
        try:
            act = PythonActivity.mActivity
            service_cls = autoclass(f"{act.getPackageName()}.ServiceMonitor")
            service_cls.start(act, "")
        except Exception:
            try:
                from android import start_service
                start_service(name="monitor", title="الحظر المشروط", description="الخدمة تعمل في الخلفية")
            except Exception:
                pass


if __name__ == "__main__":
    CondBlockerApp().run()
