import os
import sys
import threading
import traceback
from datetime import datetime


# ---------------------------------------------------------------------------
# Kivy 2.3 يعطي كل عنصر BoxShadow يرسم ظلاً عبر FBO و shader خاص. على هذا
# الجهاز يفشل بناء الـ shader، فينهار Kivy داخل
# Shader.get_program_log برسالة
#   SystemError: Negative size passed to PyBytes_FromStringAndSize
# ثم يحاول كل BoxShadow العمل على FBO = None فيفيض السجل بمئات الأخطاء:
#   BoxShadow._update_canvas -> 'NoneType' object has no attribute 'pos'
#   BoxShadow._update_fbo    -> 'NoneType' object has no attribute 'x'
#   BoxShadow._update_shadow -> 'NoneType' object does not support item ...
# وKivyMD يضع BoxShadow على كل Card وButton وDialog عبر
# CommonElevationBehavior، لذلك نستبدل الصنف بتعليمة خاملة لا ترسم شيئاً.
# لا بد أن ينفذ هذا قبل استيراد kivymd: مترجم KV يحل اسم الصنف من Factory
# لحظة تحليل قاعدة الـ KV وليس عند إنشاء العنصر.
# ---------------------------------------------------------------------------
def _disable_box_shadows():
    try:
        from kivy.factory import Factory
        from kivy.graphics import InstructionGroup

        class FlatShadow(InstructionGroup):
            """بديل خامل لـ kivy.graphics.boxshadow.BoxShadow."""

            def __init__(self, **kwargs):
                super().__init__()

            def add(self, *instructions):
                return None

        # الخصائص التي تحددها قاعدة KivyMD عند كل إعادة رسم.
        FlatShadow.pos = None
        FlatShadow.size = None
        FlatShadow.offset = None
        FlatShadow.blur_radius = None
        FlatShadow.spread_radius = None
        FlatShadow.border_radius = None

        Factory.register("BoxShadow", cls=FlatShadow)
        print("[GRAPHICS] BoxShadow disabled (flat UI, no FBO/shader)")
        return True
    except Exception:
        traceback.print_exc()
        return False


_DISABLE_BOX_SHADOWS = _disable_box_shadows()

from kivy.clock import Clock
from kivy.core.text import LabelBase
from kivy.core.window import Window
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.utils import get_color_from_hex
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDFlatButton, MDIconButton, MDRaisedButton
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDLabel
from kivymd.uix.progressbar import MDProgressBar
from kivymd.uix.screen import MDScreen
from kivymd.uix.screenmanager import MDScreenManager
from kivymd.uix.selectioncontrol import MDCheckbox, MDSwitch
from kivymd.uix.textfield import MDTextField

from database import (
    COOLDOWN_24H_SECONDS,
    GRACE_WINDOW_30M_SECONDS,
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
        text = str(text)
        has_arabic = any("\u0600" <= c <= "\u06FF" or "\u0750" <= c <= "\u077F" for c in text)
        if not has_arabic:
            return text
        try:
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception:
            return text
except Exception:
    def ar(text: str) -> str:
        return str(text) if text else ""


def _dur(seconds: float) -> str:
    """وصف المدة بالعربية (ثانية / دقيقة / ساعة) حتى تطابق النصوص القيم الفعلية."""
    total = int(round(float(seconds)))
    if total < 60:
        return f"{total} ثانية"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes} دقيقة"
    hours = minutes // 60
    rest = minutes % 60
    return f"{hours} ساعة" if rest == 0 else f"{hours} ساعة و{rest} دقيقة"


CD_LABEL = _dur(COOLDOWN_24H_SECONDS)      # "10 ثانية" في وضع التصحيح
GR_LABEL = _dur(GRACE_WINDOW_30M_SECONDS)  # "30 دقيقة"


if IS_ANDROID:
    try:
        from jnius import autoclass, cast

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
        JString = autoclass("java.lang.String")
        Toast = autoclass("android.widget.Toast")
    except Exception:
        traceback.print_exc()


def _find_arabic_font() -> tuple[str | None, str | None]:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_reg = os.path.join(base_dir, "assets", "fonts", "arabic.ttf")
    bundled_bold = os.path.join(base_dir, "assets", "fonts", "arabic_bold.ttf")
    if os.path.exists(bundled_reg) and os.path.getsize(bundled_reg) > 10000:
        bold = (
            bundled_bold
            if os.path.exists(bundled_bold) and os.path.getsize(bundled_bold) > 10000
            else bundled_reg
        )
        return bundled_reg, bold

    font_dirs = ["/system/fonts", "/system/product/fonts"]
    preferred = [
        "NotoNaskhArabic-Regular.ttf",
        "NotoSansArabic-Regular.ttf",
        "DroidSansArabic.ttf",
        "SamsungOneArabic-Regular.ttf",
    ]
    for d in font_dirs:
        for name in preferred:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p, p
    for d in font_dirs:
        if os.path.isdir(d):
            for fname in sorted(os.listdir(d)):
                low = fname.lower()
                if "arabic" in low and low.endswith(".ttf") and "-vf" not in low:
                    p = os.path.join(d, fname)
                    return p, p
    return None, None


ARABIC_FONT = "Roboto"
_arabic_font_path, _arabic_font_bold_path = _find_arabic_font()
if _arabic_font_path:
    try:
        LabelBase.register(
            name="ArabicUI",
            fn_regular=_arabic_font_path,
            fn_bold=_arabic_font_bold_path or _arabic_font_path,
        )
        ARABIC_FONT = "ArabicUI"
        # Register over Roboto as well so every internal Kivy/KivyMD widget renders Arabic & Latin
        LabelBase.register(
            name="Roboto",
            fn_regular=_arabic_font_path,
            fn_bold=_arabic_font_bold_path or _arabic_font_path,
            fn_italic=_arabic_font_path,
            fn_bolditalic=_arabic_font_bold_path or _arabic_font_path,
        )
    except Exception:
        traceback.print_exc()

print(f"[FONT] using: {ARABIC_FONT} from {_arabic_font_path}")


THEME_HEX = {
    "bg_primary": "#080C16",
    "bg_secondary": "#0E1626",
    "card": "#141D31",
    "card_hover": "#1B2740",
    "border": "#233149",
    "accent": "#FF7A18",
    "accent_soft": "#FFB067",
    "cyan": "#22D3EE",
    "warning": "#F59E0B",
    "text": "#F2F6FC",
    "muted": "#93A4BF",
    "success": "#22C55E",
    "danger": "#F43F5E",
}

THEME = {k: get_color_from_hex(v) for k, v in THEME_HEX.items()}


KV = f"""
#:import get_color_from_hex kivy.utils.get_color_from_hex

<DarkCard@MDCard>:
    md_bg_color: get_color_from_hex("{THEME_HEX['card']}")
    line_color: get_color_from_hex("{THEME_HEX['border']}")
    line_width: dp(1)
    radius: [dp(22), dp(22), dp(22), dp(22)]
    padding: dp(18)
    spacing: dp(12)
    size_hint_y: None
    height: self.minimum_height

<PillLabel@MDLabel>:
    font_name: "{ARABIC_FONT}"
    markup: False
    halign: "right"
    valign: "middle"
    size_hint_y: None
    height: dp(24)

<OnboardingScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
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
            text_color: get_color_from_hex("{THEME_HEX['text']}")
            halign: "center"
            size_hint_y: None
            height: dp(38)

        MDLabel:
            id: lbl_sub
            text: ""
            font_name: "{ARABIC_FONT}"
            font_size: "13sp"
            theme_text_color: "Custom"
            text_color: get_color_from_hex("{THEME_HEX['muted']}")
            halign: "center"
            size_hint_y: None
            height: dp(48)

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
            md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
            text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
            size_hint_x: 1
            height: dp(48)
            disabled: True
            on_release: root.enter_dashboard()

<DashboardScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(68)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(18), dp(12)]
            spacing: dp(12)

            MDIconButton:
                icon: "cog"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['accent']}")
                on_release: app.go_screen("settings")

            MDLabel:
                id: dash_header
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "19sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: get_color_from_hex("{THEME_HEX['text']}")

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
                            text_color: get_color_from_hex("{THEME_HEX['warning']}")
                    MDLabel:
                        id: lock_desc
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                        text_size: self.width, None
                    MDRaisedButton:
                        id: btn_lock_action
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        on_release: root.on_lock_action()

                MDBoxLayout:
                    size_hint_y: None
                    height: dp(42)
                    MDRaisedButton:
                        id: btn_add_rule
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        on_release: root.open_new_rule()
                    MDLabel:
                        id: rules_section_lbl
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "16sp"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['text']}")

                MDBoxLayout:
                    id: rules_container
                    orientation: "vertical"
                    spacing: dp(12)
                    size_hint_y: None
                    height: self.minimum_height

<RuleBuilderScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(68)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(18), dp(12)]
            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['text']}")
                on_release: app.go_screen("dashboard")
            MDLabel:
                id: rb_title
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "18sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: get_color_from_hex("{THEME_HEX['text']}")

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
                        text_color: get_color_from_hex("{THEME_HEX['text']}")
                        size_hint_y: None
                        height: dp(24)
                    MDTextField:
                        id: input_rule_name
                        font_name: "{ARABIC_FONT}"
                        mode: "rectangle"

                DarkCard:
                    orientation: "vertical"
                    MDLabel:
                        id: lbl_target_sec
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['warning']}")
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_target_summary
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                        text_size: self.width, None
                    MDRaisedButton:
                        id: btn_pick_targets
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
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
                        text_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_source_summary
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                        text_size: self.width, None
                    MDRaisedButton:
                        id: btn_pick_sources
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
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
                        text_color: get_color_from_hex("{THEME_HEX['text']}")
                        size_hint_y: None
                        height: dp(26)
                    MDTextField:
                        id: input_minutes
                        font_name: "{ARABIC_FONT}"
                        input_filter: "int"
                        text: "20"
                        mode: "rectangle"

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(44)
                        spacing: dp(10)
                        MDSwitch:
                            id: switch_mode
                            active: False
                            on_active: root.on_mode_switch(self.active)
                        MDLabel:
                            id: lbl_mode_title
                            text: ""
                            font_name: "{ARABIC_FONT}"
                            halign: "right"
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")

                    MDLabel:
                        id: lbl_mode_desc
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                        text_size: self.width, None

                MDRaisedButton:
                    id: btn_save_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
                    text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                    size_hint_x: 1
                    height: dp(48)
                    on_release: root.save_rule()

                MDRaisedButton:
                    id: btn_delete_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    md_bg_color: get_color_from_hex("{THEME_HEX['danger']}")
                    text_color: get_color_from_hex("{THEME_HEX['text']}")
                    size_hint_x: 1
                    height: dp(44)
                    opacity: 0
                    disabled: True
                    on_release: root.delete_current_rule()

<SettingsScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(68)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(18), dp(12)]
            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['text']}")
                on_release: app.go_screen("dashboard")
            MDLabel:
                id: settings_title
                text: ""
                font_name: "{ARABIC_FONT}"
                font_size: "18sp"
                bold: True
                halign: "right"
                theme_text_color: "Custom"
                text_color: get_color_from_hex("{THEME_HEX['text']}")

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
                        text_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_reset_val
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "14sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['text']}")
                        size_hint_y: None
                        height: dp(28)
                    MDRaisedButton:
                        id: btn_pick_reset_time
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
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
                        text_color: get_color_from_hex("{THEME_HEX['warning']}")
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_strict_status
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                    MDRaisedButton:
                        id: btn_strict_unlock
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
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
                        text_color: get_color_from_hex("{THEME_HEX['text']}")
                        size_hint_y: None
                        height: dp(26)
                    MDRaisedButton:
                        id: btn_recheck_perms
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
                        text_color: get_color_from_hex("{THEME_HEX['cyan']}")
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
                        text_color: get_color_from_hex("{THEME_HEX['danger']}")
                        size_hint_y: None
                        height: dp(26)
                    MDLabel:
                        id: lbl_tamper_logs
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12sp"
                        halign: "right"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                        size_hint_y: None
                        height: self.texture_size[1] + dp(10)
                        text_size: self.width, None
"""


def format_hhmmss(seconds: float) -> str:
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def notify_snack(msg: str):
    """إشعار سريع ومستقر بدون تعارض بين إصدارات MDSnackbar."""
    if IS_ANDROID:
        try:
            act = PythonActivity.mActivity
            if act is not None:
                from jnius import PythonJavaClass, java_method

                class _ToastRunnable(PythonJavaClass):
                    __javainterfaces__ = ["java/lang/Runnable"]
                    __javacontext__ = "app"

                    def __init__(self, text):
                        super().__init__()
                        self.text = text

                    @java_method("()V")
                    def run(self):
                        try:
                            Toast.makeText(
                                act.getApplicationContext(),
                                JString(str(self.text)),
                                Toast.LENGTH_SHORT,
                            ).show()
                        except Exception:
                            pass

                act.runOnUiThread(_ToastRunnable(msg))
                return
        except Exception:
            traceback.print_exc()
    try:
        dlg = MDDialog(
            title=ar("تنبيه"),
            text=ar(msg),
            md_bg_color=THEME["card"],
            buttons=[
                MDFlatButton(
                    text=ar("حسناً"),
                    font_name=ARABIC_FONT,
                    on_release=lambda *a: dlg.dismiss(),
                )
            ],
        )
        dlg.open()
    except Exception:
        print(f"[NOTIFY] {msg}")


class PermissionBridge:
    """فحص وتوجيه المستخدم لتفعيل الصلاحيات الأربع الأساسية."""

    @staticmethod
    def has_usage_access() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            if act is None:
                return False
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
            act = PythonActivity.mActivity
            if act is None:
                return False
            return bool(Settings.canDrawOverlays(act))
        except Exception:
            return False

    @staticmethod
    def has_accessibility_enabled() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            if act is None:
                return False
            my_pkg = str(act.getPackageName())
            am = act.getSystemService(Context.ACCESSIBILITY_SERVICE)
            if am is not None:
                enabled_list = am.getEnabledAccessibilityServiceList(-1)
                if enabled_list is not None:
                    for i in range(enabled_list.size()):
                        info = enabled_list.get(i)
                        sid = str(info.getId())
                        if my_pkg in sid and "AppBlockerAccessibilityService" in sid:
                            return True

            services = Settings.Secure.getString(
                act.getContentResolver(),
                Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
            )
            if services:
                s_str = str(services)
                if my_pkg in s_str and "AppBlockerAccessibilityService" in s_str:
                    return True
            return False
        except Exception:
            return False

    @staticmethod
    def has_device_admin() -> bool:
        if not IS_ANDROID:
            return True
        try:
            act = PythonActivity.mActivity
            if act is None:
                return False
            dpm = act.getSystemService(Context.DEVICE_POLICY_SERVICE)
            comp = ComponentName(
                act.getPackageName(),
                "com.tareq.motiv_force.AppDeviceAdminReceiver",
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
        try:
            act = PythonActivity.mActivity
            intent = Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            act.startActivity(intent)
        except Exception as e:
            notify_snack(f"تعذر فتح الإعدادات: {e}")

    @staticmethod
    def open_overlay_settings():
        if not IS_ANDROID:
            return
        try:
            act = PythonActivity.mActivity
            intent = Intent(
                Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse(f"package:{act.getPackageName()}"),
            )
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            act.startActivity(intent)
        except Exception as e:
            notify_snack(f"تعذر فتح الإعدادات: {e}")

    @staticmethod
    def open_accessibility_settings():
        if not IS_ANDROID:
            return
        try:
            act = PythonActivity.mActivity
            intent = Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            act.startActivity(intent)
        except Exception as e:
            notify_snack(f"تعذر فتح الإعدادات: {e}")

    @staticmethod
    def request_device_admin():
        if not IS_ANDROID:
            return
        try:
            act = PythonActivity.mActivity
            comp = ComponentName(
                act.getPackageName(),
                "com.tareq.motiv_force.AppDeviceAdminReceiver",
            )
            intent = Intent(DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
            intent.putExtra(
                DevicePolicyManager.EXTRA_DEVICE_ADMIN,
                cast("android.os.Parcelable", comp),
            )
            explanation = JString(f"مطلوب لمنع حذف التطبيق أو تعطيله خارج نافذة الـ {GR_LABEL} المسموحة.")
            intent.putExtra(
                DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                cast("java.lang.CharSequence", explanation),
            )
            act.startActivity(intent)
        except Exception as e:
            notify_snack(f"تعذر فتح شاشة مشرف الجهاز: {e}")


class OnboardingScreen(MDScreen):
    def on_pre_enter(self, *args):
        self.ids.lbl_title.text = ar("تهيئة الصلاحيات الأساسية")
        self.ids.lbl_sub.text = ar(
            "لضمان عمل الحظر المشروط والحماية الصارمة دون توقف، يجب تفعيل الصلاحيات الأربع التالية:"
        )
        self.ids.btn_continue.text = ar("متابعة إلى لوحة التحكم")
        self._last_perm_state = None
        self.refresh_permissions(force=True)
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_permissions(), 1.5)

    def on_pre_leave(self, *args):
        if hasattr(self, "_ev") and self._ev:
            self._ev.cancel()

    def refresh_permissions(self, force: bool = False):
        try:
            state_tuple = (
                PermissionBridge.has_usage_access(),
                PermissionBridge.has_overlay_permission(),
                PermissionBridge.has_accessibility_enabled(),
                PermissionBridge.has_device_admin(),
            )
            if not force and getattr(self, "_last_perm_state", None) == state_tuple:
                return
            self._last_perm_state = state_tuple

            container = self.ids.perm_list
            container.clear_widgets()

            items = [
                (
                    "الوصول لبيانات الاستخدام (Usage Access)",
                    "لقراءة دقائق استخدام التطبيقات بدقة يومياً.",
                    state_tuple[0],
                    PermissionBridge.open_usage_settings,
                ),
                (
                    "الظهور فوق التطبيقات (Display Over Other Apps)",
                    "لعرض شاشة الحظر الفورية عند فتح تطبيق مقيد.",
                    state_tuple[1],
                    PermissionBridge.open_overlay_settings,
                ),
                (
                    "خدمة إمكانية الوصول (Accessibility Service)",
                    "للمراقبة الفورية ومنع الإيقاف الإجباري أو سحب الصلاحيات.",
                    state_tuple[2],
                    PermissionBridge.open_accessibility_settings,
                ),
                (
                    "مشرف الجهاز (Device Admin)",
                    "لمنع إلغاء تثبيت التطبيق أثناء حالة القفل الصارم.",
                    state_tuple[3],
                    PermissionBridge.request_device_admin,
                ),
            ]

            all_ok = all(state_tuple)
            for title, desc, granted, action in items:
                card = MDCard(
                    md_bg_color=THEME["card"],
                    radius=[dp(14), dp(14), dp(14), dp(14)],
                    padding=dp(14),
                    spacing=dp(8),
                    orientation="vertical",
                    size_hint_y=None,
                    height=dp(136),
                )
                top = MDBoxLayout(size_hint_y=None, height=dp(28))
                status_lbl = MDLabel(
                    text=ar("مفعلة" if granted else "غير مفعلة"),
                    font_name=ARABIC_FONT,
                    bold=True,
                    font_size="13sp",
                    theme_text_color="Custom",
                    text_color=THEME["success"] if granted else THEME["warning"],
                    size_hint_x=0.32,
                )
                title_lbl = MDLabel(
                    text=ar(title),
                    font_name=ARABIC_FONT,
                    bold=True,
                    font_size="14sp",
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["text"],
                    size_hint_x=0.68,
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
                    height=dp(36),
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
        except Exception:
            traceback.print_exc()

    def enter_dashboard(self):
        app = MDApp.get_running_app()
        app.db.set_onboarding_complete(True)
        app.start_background_service()
        app.go_screen("dashboard")


class DashboardScreen(MDScreen):
    _busy = False
    _label_cache: dict[str, str] = {}

    def on_pre_enter(self, *args):
        self.ids.dash_header.text = ar("لوحة التحكم")
        self.ids.btn_add_rule.text = ar("+ قاعدة جديدة")
        self.ids.rules_section_lbl.text = ar("القواعد النشطة")
        self.refresh_dashboard(force=True)
        if getattr(self, "_ev", None):
            try:
                self._ev.cancel()
            except Exception:
                pass
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_dashboard(), 2.0)

    def on_pre_leave(self, *args):
        if getattr(self, "_ev", None):
            self._ev.cancel()
            self._ev = None

    @staticmethod
    def _new_card():
        """بطاقة بنفس شكل DarkCard في ملف KV لكن متاحة من بايثون."""
        return MDCard(
            md_bg_color=THEME["card"],
            line_color=THEME.get("border", THEME["card"]),
            line_width=dp(1),
            radius=[dp(22), dp(22), dp(22), dp(22)],
            padding=dp(18),
            spacing=dp(12),
            orientation="vertical",
            size_hint_y=None,
            height=dp(10),
        )

    def _grow(self, card):
        """اجعل البطاقة تأخذ ارتفاع محتواها بدل ارتفاع ثابت يسبب تراكب النصوص."""
        card.height = card.minimum_height
        card.bind(minimum_height=card.setter("height"))
        return card

    def _label(self, pkg: str) -> str:
        if not pkg:
            return ""
        if pkg not in self._label_cache:
            try:
                self._label_cache[pkg] = MDApp.get_running_app().tracker.get_app_label(pkg)
            except Exception:
                self._label_cache[pkg] = pkg
        return self._label_cache[pkg]

    # ------------------------------------------------------------------ data
    def refresh_dashboard(self, force: bool = False):
        """تحديث اللوحة دون تجميد الواجهة.

        سابقاً كان استعلام UsageStats (ساعات من الأحداث) وPackageManager لكل
        تطبيق مختار يجري على خيط الواجهة، فتتجمد الشاشة كلما زاد عدد
        التطبيقات المختارة. الآن الحساب في خيط منفصل والعرض بعد وصوله.
        """
        if self._busy and not force:
            return
        self._busy = True
        app = MDApp.get_running_app()

        lock_info = {}
        try:
            now_elapsed, now_wall = get_android_clocks()
            lock_info = app.db.sync_monotonic_clock(now_elapsed, now_wall)
        except Exception:
            traceback.print_exc()

        def worker():
            try:
                evaluations, blocked_targets, usage = app.tracker.evaluate_all_rules(app.db)
                err = ""
            except Exception:
                evaluations, blocked_targets, usage = [], {}, {}
                err = traceback.format_exc()
                print(f"[DASHBOARD] evaluate failed:\n{err}")
            Clock.schedule_once(
                lambda dt: self._apply(
                    evaluations, blocked_targets, usage, err, lock_info, force
                ),
                0,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, evaluations, blocked_targets, usage, err, lock_info, force_render=False):
        self._busy = False
        try:
            app = MDApp.get_running_app()
            if lock_info:
                self._render_lock(lock_info)
            self.ids.btn_add_rule.disabled = False

            # لا تُعد بناء البطاقات كل ثانيتين إن لم تتغير البيانات:
            # إعادة بناء عشرات البطاقات دورياً كانت تسبب تقطعاً في التمرير.
            state_key = lock_info.get("state") if lock_info else ""
            signature = (
                state_key,
                bool(err),
                tuple(
                    (
                        ev["rule"]["id"],
                        ev["rule"]["enabled"],
                        ev["is_unlocked"],
                        int(ev["overall_progress"] * 100),
                        round(float(ev["completed_minutes"]), 1),
                    )
                    for ev in evaluations
                ),
            )
            if signature != getattr(self, "_rules_signature", None) or force_render:
                self._rules_signature = signature
                self._render_rules(evaluations, err, usage)
            try:
                app.db.export_blocker_state(
                    app.state_json_path, list(blocked_targets.keys())
                )
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()

    def _render_lock(self, lock_info):
        state = lock_info["state"]
        rem = lock_info["remaining_seconds"]

        if state == LOCK_STATE_LOCKED:
            self.ids.lock_badge.text = ar("الحماية الصارمة مفعّلة")
            self.ids.lock_badge.text_color = THEME["warning"]
            self.ids.lock_desc.text = ar(
                f"لا يمكن حذف القواعد أو تعديلها. لفتح نافذة تعديل {GR_LABEL} ابدأ عداد {CD_LABEL}."
            )
            self.ids.btn_lock_action.text = ar(f"طلب فتح نافذة تعديل ({CD_LABEL})")
            self.ids.btn_lock_action.md_bg_color = THEME["accent"]
        elif state == LOCK_STATE_WAITING_24H:
            self.ids.lock_badge.text = ar(f"فك القفل بعد {format_hhmmss(rem)}")
            self.ids.lock_badge.text_color = THEME["cyan"]
            self.ids.lock_desc.text = ar(
                f"القيود كلها مفعّلة حتى ينتهي العد، ثم تُفتح نافذة تعديل {GR_LABEL} فقط."
            )
            self.ids.btn_lock_action.text = ar("إلغاء الطلب والعودة للقفل الفوري")
            self.ids.btn_lock_action.md_bg_color = THEME["danger"]
        else:
            self.ids.lock_badge.text = ar(f"نافذة التعديل مفتوحة {format_hhmmss(rem)}")
            self.ids.lock_badge.text_color = THEME["success"]
            self.ids.lock_desc.text = ar(
                "يمكنك الآن تعديل القواعد أو حذفها قبل انتهاء المهلة."
            )
            self.ids.btn_lock_action.text = ar("إعادة القفل الصارم الآن")
            self.ids.btn_lock_action.md_bg_color = THEME["cyan"]

    def _render_rules(self, evaluations, err, usage):
        container = self.ids.rules_container
        container.clear_widgets()

        try:
            if not PermissionBridge.all_granted():
                warn = self._new_card()
                warn.add_widget(
                    MDLabel(
                        text=ar("الحماية غير مكتملة: صلاحية ناقصة"),
                        font_name=ARABIC_FONT,
                        bold=True,
                        font_size="14sp",
                        halign="right",
                        theme_text_color="Custom",
                        text_color=THEME["warning"],
                        size_hint_y=None,
                        height=dp(26),
                    )
                )
                warn.add_widget(
                    MDLabel(
                        text=ar(
                            "بدون صلاحية الاستخدام + الظهور فوق التطبيقات + خدمة الوصول "
                            "لن يعمل المنع فعلياً. اضغط الزر لإكمالها."
                        ),
                        font_name=ARABIC_FONT,
                        font_size="12sp",
                        halign="right",
                        theme_text_color="Custom",
                        text_color=THEME["muted"],
                        size_hint_y=None,
                        height=dp(58),
                    )
                )
                warn.add_widget(
                    MDRaisedButton(
                        text=ar("إكمال الصلاحيات"),
                        font_name=ARABIC_FONT,
                        md_bg_color=THEME["warning"],
                        text_color=THEME["bg_primary"],
                        size_hint_x=1,
                        on_release=lambda x: MDApp.get_running_app().go_screen("onboarding"),
                    )
                )
                container.add_widget(self._grow(warn))
        except Exception:
            traceback.print_exc()

        if err:
            card = self._new_card()
            card.add_widget(
                MDLabel(
                    text=ar("تعذر حساب القواعد. تفاصيل الخطأ:"),
                    font_name=ARABIC_FONT,
                    bold=True,
                    font_size="13sp",
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["danger"],
                    size_hint_y=None,
                    height=dp(24),
                )
            )
            card.add_widget(
                MDLabel(
                    text=err[-600:],
                    font_name=ARABIC_FONT,
                    font_size="10sp",
                    halign="left",
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                    size_hint_y=None,
                    height=dp(180),
                )
            )
            container.add_widget(self._grow(card))
            return

        if not evaluations:
            card = self._new_card()
            card.add_widget(
                MDLabel(
                    text=ar("لا توجد قواعد بعد"),
                    font_name=ARABIC_FONT,
                    bold=True,
                    font_size="16sp",
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["text"],
                    size_hint_y=None,
                    height=dp(30),
                )
            )
            card.add_widget(
                MDLabel(
                    text=ar(
                        "اضغط (+ قاعدة جديدة): اختر التطبيقات التي تريد حظرها، "
                        "والتطبيقات التي يجب استخدامها أولاً، ثم عدد الدقائق المطلوبة."
                    ),
                    font_name=ARABIC_FONT,
                    font_size="12sp",
                    halign="right",
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                    size_hint_y=None,
                    height=dp(70),
                )
            )
            container.add_widget(self._grow(card))
            return

        for ev in evaluations:
            container.add_widget(self._rule_card(ev, usage))

    def _rule_card(self, ev, usage):
        rule = ev["rule"]
        is_unlocked = ev["is_unlocked"]
        prog = float(ev["overall_progress"])
        mode_ar = "تجميعي SUM" if rule["mode"] == "SUM" else "فردي INDIVIDUAL"
        can_mod = MDApp.get_running_app().db.can_modify_restrictions()

        card = self._new_card()
        card.orientation = "vertical"

        top = MDBoxLayout(size_hint_y=None, height=dp(32), spacing=dp(10))
        chip = MDCard(
            md_bg_color=THEME["success"] if is_unlocked else THEME["danger"],
            radius=[dp(14), dp(14), dp(14), dp(14)],
            padding=0,
            size_hint=(None, None),
            size=(dp(74), dp(26)),
            pos_hint={"center_y": 0.5},
        )
        chip.add_widget(
            MDLabel(
                text=ar("مفتوح" if is_unlocked else "محظور"),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="11sp",
                halign="center",
                theme_text_color="Custom",
                text_color=get_color_from_hex("#07101E"),
            )
        )
        top.add_widget(chip)
        top.add_widget(
            MDLabel(
                text=ar(rule["name"]),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="17sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["text"],
                size_hint_y=None,
                height=dp(30),
                text_size=(self.width, None),
            )
        )

        tgt = ", ".join(self._label(p) for p in rule["target_packages"][:3])
        extra = len(rule["target_packages"]) - 3
        more = f" +{extra}" if extra > 0 else ""
        src = ", ".join(self._label(p) for p in rule["source_packages"][:3])
        meta = MDLabel(
            text=ar(f"المحظور: {tgt}{more}\nالأولوية: {src} · {mode_ar}"),
            font_name=ARABIC_FONT,
            font_size="12sp",
            halign="right",
            theme_text_color="Custom",
            text_color=THEME["muted"],
            size_hint_y=None,
            height=dp(46),
        )

        done = float(ev["completed_minutes"])
        need = float(ev["required_minutes"])
        stat = MDBoxLayout(size_hint_y=None, height=dp(28), spacing=dp(6))
        stat.add_widget(
            MDLabel(
                text=ar(f"{done:.1f}"),
                font_name=ARABIC_FONT,
                bold=True,
                font_size="19sp",
                halign="left",
                theme_text_color="Custom",
                text_color=THEME["accent_soft"],
                size_hint_x=0.28,
            )
        )
        stat.add_widget(
            MDLabel(
                text=ar(f"من {need:.0f} دقيقة مطلوبة اليوم"),
                font_name=ARABIC_FONT,
                font_size="12sp",
                halign="right",
                theme_text_color="Custom",
                text_color=THEME["muted"],
                size_hint_x=0.72,
            )
        )

        pbar = MDProgressBar(
            value=int(prog * 100),
            color=THEME["success"] if is_unlocked else THEME["accent"],
            size_hint_y=None,
            height=dp(10),
        )

        actions = MDBoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
        if can_mod:
            actions.add_widget(
                MDRaisedButton(
                    text=ar("تعديل القاعدة"),
                    font_name=ARABIC_FONT,
                    font_size="12sp",
                    md_bg_color=THEME["card_hover"],
                    text_color=THEME["accent_soft"],
                    on_release=lambda x, rid=rule["id"]: self.open_edit_rule(rid),
                )
            )
        else:
            actions.add_widget(
                MDLabel(
                    text=ar(f"التعديل يتطلب نافذة {GR_LABEL}"),
                    font_name=ARABIC_FONT,
                    font_size="11sp",
                    halign="right",
                    valign="middle",
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                )
            )
        # KivyMD 1.2.0: تمرير active إلى MDSwitch في المُنشئ يرفع
        # AttributeError لأن on_active ينفذ قبل إنشاء ids.thumb، فيتوقف
        # بناء كل بطاقات القواعد وتظهر اللوحة فارغة. لذلك نضبطها بعد الإنشاء.
        sw = MDSwitch()
        sw.disabled = not can_mod
        sw.active = bool(rule["enabled"])
        sw.bind(active=lambda inst, val, rid=rule["id"]: self.on_toggle_rule(rid, val))
        actions.add_widget(sw)

        card.add_widget(top)
        card.add_widget(meta)
        card.add_widget(stat)
        card.add_widget(pbar)
        card.add_widget(actions)
        return self._grow(card)

    def on_lock_action(self):
        app = MDApp.get_running_app()
        now_elapsed, now_wall = get_android_clocks()
        state = app.db.get_lock_state()["state"]
        if state == LOCK_STATE_LOCKED:
            app.db.request_unlock_24h(now_elapsed, now_wall)
            notify_snack(f"بدأ العداد التنازلي لفتح نافذة التعديل: {CD_LABEL}.")
        else:
            app.db.cancel_unlock_request()
            notify_snack("تم تفعيل القفل الصارم فوراً.")
        app.sync_service_state()
        self.refresh_dashboard()

    def on_toggle_rule(self, rule_id: int, val: bool):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("لا يمكن تعطيل القواعد أثناء القفل الصارم.")
            self.refresh_dashboard()
            return
        app.db.toggle_rule(rule_id, val)
        app.sync_service_state()
        self.refresh_dashboard()

    def open_new_rule(self):
        # إضافة قاعدة جديدة مسموحة دائماً: التشديد لا يخالف الحماية الصارمة،
        # بينما التعديل والحذف يبقيان محظورين خارج نافذة الـ 30 دقيقة.
        app = MDApp.get_running_app()
        rb = app.root.get_screen("rule_builder")
        rb.load_rule(None)
        app.go_screen("rule_builder")

    def open_edit_rule(self, rule_id: int):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack(f"التعديل متاح فقط خلال نافذة الـ {GR_LABEL}.")
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
    _label_cache: dict[str, str] = {}

    def on_pre_leave(self, *args):
        # لا تترك نافذة الاختيار مفتوحة إذا غادر المستخدم الشاشة
        try:
            if self.picker_dialog is not None:
                self.picker_dialog.dismiss()
                self.picker_dialog = None
        except Exception:
            pass

    def _label(self, pkg: str) -> str:
        if pkg not in self._label_cache:
            try:
                self._label_cache[pkg] = MDApp.get_running_app().tracker.get_app_label(pkg)
            except Exception:
                self._label_cache[pkg] = pkg
        return self._label_cache[pkg]

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
            try:
                self._installed_apps = app.tracker.get_installed_apps()
            except Exception:
                pass
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
            t_txt = ", ".join(self._label(p) for p in self.selected_targets)
            self.ids.lbl_target_summary.text = ar(f"المختار ({len(self.selected_targets)}): {t_txt}")
        else:
            self.ids.lbl_target_summary.text = ar("لم يتم اختيار أي تطبيق محظور بعد.")

        if self.selected_sources:
            s_txt = ", ".join(self._label(p) for p in self.selected_sources)
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
        """نافذة اختيار التطبيقات — لا تجمّد الواجهة ولا تفشل بصمت.

        سابقاً كانت تجلب قائمة التطبيقات على خيط الواجهة (تجميد عدة ثوانٍ ثم
        عودة بلا نتيجة إن فشل شيء)، وكانت تبني مئات الصفوف دفعة واحدة.
        الآن: الجلب في خيط منفصل، والرسم على دفعات 12 صفاً لكل إطار.
        """
        app = MDApp.get_running_app()
        temp_selected = set(
            self.selected_targets if role == "target" else self.selected_sources
        )

        status_lbl = MDLabel(
            text=ar("جارٍ تحميل قائمة التطبيقات..."),
            font_name=ARABIC_FONT,
            font_size="12sp",
            halign="right",
            size_hint_y=None,
            height=dp(26),
            theme_text_color="Custom",
            text_color=THEME["muted"],
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

        content = MDBoxLayout(
            orientation="vertical",
            spacing=dp(8),
            size_hint_y=None,
            height=dp(430),
        )
        content.add_widget(search_field)
        content.add_widget(status_lbl)
        content.add_widget(scroll)

        state = {"apps": [], "query": "", "gen": 0, "matched": [], "index": 0}

        def build_row(item):
            pkg = item["package_name"]

            def _on_active(inst, val, p=item["package_name"]):
                if val:
                    temp_selected.add(p)
                else:
                    temp_selected.discard(p)
                status_lbl.text = ar(f"المحدد: {len(temp_selected)}")

            row = MDBoxLayout(
                orientation="horizontal",
                size_hint_y=None,
                height=dp(58),
                spacing=dp(10),
                padding=[dp(6), 0],
            )
            cb = MDCheckbox(
                active=pkg in temp_selected,
                size_hint=(None, None),
                size=(dp(34), dp(34)),
                pos_hint={"center_y": 0.5},
            )
            cb.bind(active=_on_active)
            row.add_widget(cb)

            text = (
                f"[b][color={THEME_HEX['text']}]{ar(item['name'])}[/color][/b]\n"
                f"[color={THEME_HEX['muted']}]{pkg}[/color]"
            )
            row.add_widget(
                MDLabel(
                    text=text,
                    markup=True,
                    font_name=ARABIC_FONT,
                    font_size="13sp",
                    halign="right",
                    valign="middle",
                    theme_text_color="Custom",
                    text_color=THEME["text"],
                )
            )

            icon_path = item.get("icon_path") or ""
            if icon_path and os.path.exists(icon_path):
                row.add_widget(
                    Image(
                        source=icon_path,
                        size_hint=(None, None),
                        size=(dp(30), dp(30)),
                        pos_hint={"center_y": 0.5},
                    )
                )
            return row

        def render_more(gen, *_args):
            if gen != state["gen"]:
                return
            matched = state["matched"]
            end = min(state["index"] + 20, len(matched))
            for item in matched[state["index"]:end]:
                list_box.add_widget(build_row(item))
            state["index"] = end
            status_lbl.text = ar(
                f"المعروض {end} من {len(matched)} — المحدد: {len(temp_selected)}"
            )
            if end < len(matched):
                Clock.schedule_once(lambda dt: render_more(gen), 0)

        def start_render():
            state["gen"] += 1
            gen = state["gen"]
            state["index"] = 0
            query = state["query"]
            state["matched"] = [
                item
                for item in state["apps"]
                if not query
                or query in item["name"].lower()
                or query in item["package_name"].lower()
            ]
            list_box.clear_widgets()
            if not state["matched"]:
                status_lbl.text = ar("لا توجد نتائج مطابقة.")
                return
            Clock.schedule_once(lambda dt: render_more(gen), 0)

        def apply_apps(apps):
            state["apps"] = list(apps or [])
            if state["apps"]:
                status_lbl.text = ar(f"عدد التطبيقات: {len(state['apps'])}")
                start_render()
            else:
                status_lbl.text = ar(
                    "تعذر جلب التطبيقات. تأكد من صلاحية الاستخدام (Usage Access) ثم أعد المحاولة."
                )
                list_box.clear_widgets()
                list_box.add_widget(
                    MDRaisedButton(
                        text=ar("إعادة المحاولة"),
                        font_name=ARABIC_FONT,
                        md_bg_color=THEME["accent"],
                        text_color=THEME["bg_primary"],
                        size_hint_y=None,
                        height=dp(46),
                        on_release=lambda x: load_apps(force=True),
                    )
                )

        def load_apps(force: bool = False):
            if self._installed_apps and not force:
                apply_apps(self._installed_apps)
                return
            status_lbl.text = ar("جارٍ تحميل قائمة التطبيقات...")

            def worker():
                try:
                    apps = app.tracker.get_installed_apps(force_refresh=True)
                    self._installed_apps = apps
                except Exception:
                    traceback.print_exc()
                    apps = []
                Clock.schedule_once(lambda dt: apply_apps(apps), 0)

            threading.Thread(target=worker, daemon=True).start()

        def on_search(inst, txt):
            state["query"] = (txt or "").strip().lower()
            if state["apps"]:
                start_render()

        search_field.bind(text=on_search)

        def on_confirm(*args):
            if role == "target":
                self.selected_targets = sorted(temp_selected)
            else:
                self.selected_sources = sorted(temp_selected)
            self._refresh_summaries()
            if self.picker_dialog:
                self.picker_dialog.dismiss()

        try:
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
        except Exception:
            traceback.print_exc()
            notify_snack("تعذر فتح نافذة اختيار التطبيقات.")
            return

        load_apps()

    def save_rule(self):
        app = MDApp.get_running_app()
        if self.editing_rule_id is not None and not app.db.can_modify_restrictions():
            notify_snack(f"تعديل قاعدة موجودة محظور خارج نافذة الـ {GR_LABEL}.")
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
        try:
            app.start_background_service()
        except Exception:
            traceback.print_exc()
        app.go_screen("dashboard")

    def delete_current_rule(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack(f"الحذف محظور خارج نافذة الـ {GR_LABEL}.")
            return
        if self.editing_rule_id is not None:
            app.db.delete_rule(self.editing_rule_id)
            app.sync_service_state()
            notify_snack("تم حذف القاعدة.")
            app.go_screen("dashboard")


class SettingsScreen(MDScreen):
    time_dialog = None

    def on_pre_enter(self, *args):
        self.ids.settings_title.text = ar("الإعدادات والتصفير اليومي")
        self.ids.lbl_reset_title.text = ar("وقت التصفير اليومي المخصص (Daily Reset)")
        self.ids.btn_pick_reset_time.text = ar("تغيير وقت التصفير اليومي")
        self.ids.lbl_strict_title.text = ar(f"إدارة الحماية الصارمة ({CD_LABEL} -> {GR_LABEL})")
        self.ids.lbl_perms_link.text = ar("فحص صلاحيات النظام")
        self.ids.btn_recheck_perms.text = ar("مراجعة شاشة الصلاحيات")
        self.ids.lbl_tamper_title.text = ar("سجل محاولات التلاعب بالوقت")
        self.refresh_settings()
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_settings(), 2.5)

    def on_pre_leave(self, *args):
        if hasattr(self, "_ev") and self._ev:
            self._ev.cancel()

    def refresh_settings(self):
        try:
            app = MDApp.get_running_app()
            h, m = app.db.get_reset_time()
            self.ids.lbl_reset_val.text = ar(
                f"يتم تصفير عدادات الاستخدام وإعادة القفل يومياً الساعة {h:02d}:{m:02d}"
            )
            can_mod = app.db.can_modify_restrictions()
            self.ids.btn_pick_reset_time.disabled = not can_mod

            now_elapsed, now_wall = get_android_clocks()
            lock_info = app.db.sync_monotonic_clock(now_elapsed, now_wall)
            state = lock_info["state"]
            rem = lock_info["remaining_seconds"]

            if state == LOCK_STATE_LOCKED:
                self.ids.lbl_strict_status.text = ar(
                    f"النظام مقفل حالياً. لفتح نافذة الـ {GR_LABEL} المسموح فيها بالتعديل أو إلغاء مشرف الجهاز وحذف التطبيق، ابدأ عداد الـ {CD_LABEL}."
                )
                self.ids.btn_strict_unlock.text = ar(f"بدء عداد {CD_LABEL} لفك القفل")
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
        except Exception:
            traceback.print_exc()

    def open_time_picker(self):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack(f"تغيير وقت التصفير متاح فقط في نافذة الـ {GR_LABEL}.")
            return
        h, m = app.db.get_reset_time()
        box = MDBoxLayout(
            orientation="horizontal",
            spacing=dp(12),
            size_hint_y=None,
            height=dp(72),
            padding=[dp(8), dp(8)],
        )
        tf_min = MDTextField(
            text=f"{m:02d}",
            hint_text=ar("الدقيقة (0-59)"),
            font_name=ARABIC_FONT,
            input_filter="int",
            mode="rectangle",
        )
        tf_hour = MDTextField(
            text=f"{h:02d}",
            hint_text=ar("الساعة (0-23)"),
            font_name=ARABIC_FONT,
            input_filter="int",
            mode="rectangle",
        )
        box.add_widget(tf_min)
        box.add_widget(tf_hour)

        def _save(*args):
            try:
                nh = max(0, min(23, int(tf_hour.text.strip() or "0")))
                nm = max(0, min(59, int(tf_min.text.strip() or "0")))
                app.db.set_reset_time(nh, nm)
                app.sync_service_state()
                notify_snack(f"تم ضبط وقت التصفير اليومي على {nh:02d}:{nm:02d}")
                self.refresh_settings()
                if self.time_dialog:
                    self.time_dialog.dismiss()
            except Exception:
                notify_snack("يرجى إدخال ساعة ودقيقة صحيحة.")

        self.time_dialog = MDDialog(
            title=ar("تحديد وقت التصفير اليومي (24 ساعة)"),
            type="custom",
            md_bg_color=THEME["card"],
            content_cls=box,
            buttons=[
                MDFlatButton(
                    text=ar("إلغاء"),
                    font_name=ARABIC_FONT,
                    theme_text_color="Custom",
                    text_color=THEME["muted"],
                    on_release=lambda *a: self.time_dialog.dismiss(),
                ),
                MDRaisedButton(
                    text=ar("حفظ"),
                    font_name=ARABIC_FONT,
                    md_bg_color=THEME["accent"],
                    text_color=THEME["bg_primary"],
                    on_release=_save,
                ),
            ],
        )
        self.time_dialog.open()

    def handle_strict_button(self):
        app = MDApp.get_running_app()
        now_elapsed, now_wall = get_android_clocks()
        state = app.db.get_lock_state()["state"]
        if state == LOCK_STATE_LOCKED:
            app.db.request_unlock_24h(now_elapsed, now_wall)
            notify_snack(f"بدأ عداد الانتظار الإلزامي: {CD_LABEL}.")
        else:
            app.db.cancel_unlock_request()
            notify_snack("تم إعادة تفعيل القفل الصارم.")
        app.sync_service_state()
        self.refresh_settings()


class CondBlockerApp(MDApp):
    db: CondBlockerDB = None
    tracker: UsageTracker = None
    state_json_path: str = ""
    _paused = False

    def build(self):
        try:
            self.theme_cls.theme_style = "Dark"
            self.theme_cls.primary_palette = "Orange"

            if ARABIC_FONT != "Roboto":
                for style_name, style_cfg in self.theme_cls.font_styles.items():
                    if style_name != "Icon" and isinstance(style_cfg, list) and len(style_cfg) > 0:
                        style_cfg[0] = ARABIC_FONT

            if IS_ANDROID:
                files_dir = str(PythonActivity.mActivity.getFilesDir().getAbsolutePath())
            else:
                files_dir = os.path.join(os.path.expanduser("~"), ".motiv_force")

            os.makedirs(files_dir, exist_ok=True)
            self.db = CondBlockerDB(os.path.join(files_dir, "motiv_force.db"))
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
                Clock.schedule_once(lambda dt: self.start_background_service(), 1.0)
            else:
                sm.current = "onboarding"

            self.sync_service_state()
            return sm
        except Exception:
            err_txt = traceback.format_exc()
            print(err_txt)
            err_scr = MDScreen(md_bg_color=get_color_from_hex("#0B1120"))
            sv = ScrollView()
            lbl = Label(
                text=f"Startup Error:\n{err_txt}",
                color=(1, 0.4, 0.4, 1),
                font_size="12sp",
                size_hint_y=None,
                padding=(dp(16), dp(16)),
            )
            lbl.bind(
                width=lambda inst, val: setattr(inst, "text_size", (val - dp(32), None)),
                texture_size=lambda inst, val: setattr(inst, "height", val[1] + dp(40)),
            )
            sv.add_widget(lbl)
            err_scr.add_widget(sv)
            return err_scr

    def on_start(self):
        Window.bind(on_keyboard=self._on_back_button)
        if IS_ANDROID:
            try:
                from android.permissions import request_permissions, check_permission
                if not check_permission("android.permission.POST_NOTIFICATIONS"):
                    request_permissions(["android.permission.POST_NOTIFICATIONS"], lambda *a: None)
            except Exception as e:
                print(f"[PERMS] POST_NOTIFICATIONS skipped: {e}")

    def _on_back_button(self, window, key, *args):
        if key == 27:  # زر الرجوع في أندرويد
            if self.root and hasattr(self.root, "current"):
                if self.root.current in ("rule_builder", "settings"):
                    self.go_screen("dashboard")
                    return True
                elif self.root.current == "dashboard":
                    return True
        return False

    def go_screen(self, name: str):
        if self.root and hasattr(self.root, "current"):
            self.root.current = name

    def sync_service_state(self):
        try:
            _, blocked_targets, _ = self.tracker.evaluate_all_rules(self.db)
            self.db.export_blocker_state(self.state_json_path, list(blocked_targets.keys()))
        except Exception:
            pass

    def on_pause(self):
        # أوقف مؤقتات التحديث قبل أن يفقد أندرويد سياق OpenGL، حتى لا تُعاد
        # رسم العناصر على سياق ميت.
        self._paused = True
        self._cancel_screen_timers()
        return True

    def on_resume(self):
        self._paused = False
        try:
            if IS_ANDROID and self.db:
                self.sync_service_state()
                if self.root and hasattr(self.root, "current"):
                    curr = self.root.get_screen(self.root.current)
                    if hasattr(curr, "refresh_permissions"):
                        curr.refresh_permissions(force=True)
                    elif hasattr(curr, "refresh_dashboard"):
                        curr.refresh_dashboard()
                if self.db.is_onboarding_complete():
                    self.start_background_service()
        except Exception:
            traceback.print_exc()
        self._restart_screen_timers()

    def _cancel_screen_timers(self):
        try:
            for screen in getattr(self.root, "screens", []) or []:
                ev = getattr(screen, "_ev", None)
                if ev is not None:
                    ev.cancel()
                    screen._ev = None
        except Exception:
            traceback.print_exc()

    def _restart_screen_timers(self):
        try:
            root = self.root
            if root is None or not getattr(root, "current", None):
                return
            screen = root.get_screen(root.current)
            if getattr(screen, "_ev", None) is not None:
                return
            for name, interval in (
                ("refresh_dashboard", 2.5),
                ("refresh_settings", 2.5),
                ("refresh_permissions", 1.5),
            ):
                fn = getattr(screen, name, None)
                if callable(fn):
                    fn()
                    screen._ev = Clock.schedule_interval(lambda dt, f=fn: f(), interval)
                    return
        except Exception:
            traceback.print_exc()

    def start_background_service(self):
        if not IS_ANDROID:
            return
        try:
            act = PythonActivity.mActivity
            pkg = str(act.getPackageName())
            service_cls = autoclass(f"{pkg}.ServiceMonitor")
            service_cls.start(act, "")
            return
        except Exception as e:
            print(f"[SERVICE] ServiceMonitor.start fallback: {e}")
        try:
            from android import AndroidService
            if getattr(self, "_svc", None) is None:
                self._svc = AndroidService(
                    "الحظر المشروط",
                    "الخدمة تعمل في الخلفية لحمايتك",
                )
            self._svc.start("started")
        except Exception as e:
            print(f"[SERVICE] ❌ {e}")


def _write_crash_log(tb_text: str):
    try:
        base = os.environ.get("ANDROID_PRIVATE") or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "crash_log.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat()} =====\n{tb_text}\n")
        print(f"[CRASH] written to {path}")
    except Exception:
        pass


if __name__ == "__main__":
    try:
        CondBlockerApp().run()
    except BaseException:
        _write_crash_log(traceback.format_exc())
        raise
