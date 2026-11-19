import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
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
from kivy.factory import Factory
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.properties import BooleanProperty, StringProperty
from kivy.uix.image import Image
from kivy.uix.label import Label
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.recycleview import RecycleView
from kivy.uix.scrollview import ScrollView
from kivy.utils import get_color_from_hex
from kivymd.app import MDApp
from kivymd.uix.boxlayout import MDBoxLayout
from kivymd.uix.button import MDFillRoundFlatButton, MDFlatButton, MDIconButton
from kivymd.uix.card import MDCard
from kivymd.uix.dialog import MDDialog
from kivymd.uix.label import MDIcon, MDLabel
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
from usage_tracker import (
    IS_ANDROID,
    UsageTracker,
    detach_current_thread,
    get_android_clocks,
)

try:
    import arabic_reshaper
    from bidi.algorithm import get_display

    _AR_CACHE: dict[str, str] = {}

    def ar(text: str) -> str:
        if not text:
            return ""
        text = str(text)
        cached = _AR_CACHE.get(text)
        if cached is not None:
            return cached
        has_arabic = any("\u0600" <= c <= "\u06FF" or "\u0750" <= c <= "\u077F" for c in text)
        if not has_arabic:
            return text
        try:
            reshaped = arabic_reshaper.reshape(text)
            result = get_display(reshaped)
            if len(_AR_CACHE) > 1200:
                _AR_CACHE.clear()
            _AR_CACHE[text] = result
            return result
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
        from jnius import autoclass, cast, PythonJavaClass, java_method

        PythonActivity = autoclass("org.kivy.android.PythonActivity")
        Context = autoclass("android.content.Context")
        Intent = autoclass("android.content.Intent")
        Uri = autoclass("android.net.Uri")
        Settings = autoclass("android.provider.Settings")
        AppOpsManager = autoclass("android.app.AppOpsManager")
        UsageStatsManager = autoclass("android.app.usage.UsageStatsManager")
        Process = autoclass("android.os.Process")
        System = autoclass("java.lang.System")
        ComponentName = autoclass("android.content.ComponentName")
        DevicePolicyManager = autoclass("android.app.admin.DevicePolicyManager")
        BuildVersion = autoclass("android.os.Build$VERSION")
        JString = autoclass("java.lang.String")
        Toast = autoclass("android.widget.Toast")

        _TOAST_REFS = []

        class _ToastRunnable(PythonJavaClass):
            __javainterfaces__ = ["java/lang/Runnable"]
            __javacontext__ = "app"

            def __init__(self, text: str):
                super().__init__()
                self.text = text

            @java_method("()V")
            def run(self):
                try:
                    act = PythonActivity.mActivity
                    if act is not None:
                        Toast.makeText(
                            act.getApplicationContext(),
                            JString(str(self.text)),
                            Toast.LENGTH_SHORT,
                        ).show()
                except Exception:
                    pass
                finally:
                    try:
                        _TOAST_REFS.remove(self)
                    except Exception:
                        pass

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
    "bg_primary": "#070B12",
    "bg_secondary": "#0C1420",
    "card": "#111A2A",
    "card_hover": "#182337",
    "border": "#22304A",
    "accent": "#FF8A3D",
    "accent_soft": "#FFC08A",
    "cyan": "#38E0C8",
    "warning": "#FBBF24",
    "text": "#F5F8FF",
    "muted": "#8FA2BE",
    "success": "#34D399",
    "danger": "#FB7185",
}

THEME = {k: get_color_from_hex(v) for k, v in THEME_HEX.items()}


_PICKER_STATE = {"on_toggle": None}
_ICON_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="motiv-icon")


class AppPickRow(MDBoxLayout):
    """صف واحد في قائمة اختيار التطبيقات (يُعاد استخدامه عبر RecycleView)."""

    app_name = StringProperty("")
    pkg = StringProperty("")
    selected = BooleanProperty(False)
    icon_path = StringProperty("")

    def on_checkbox(self, active: bool):
        if active == self.selected:
            return
        self.selected = active
        cb = _PICKER_STATE.get("on_toggle")
        if cb is not None:
            try:
                cb(self.pkg, active)
            except Exception:
                traceback.print_exc()

    def on_pkg(self, *args):
        pkg = self.pkg
        self.icon_path = ""
        if pkg:
            Clock.schedule_once(lambda dt: self._load_icon(pkg), 0)

    def _load_icon(self, pkg: str):
        if pkg != self.pkg:
            return
        app = MDApp.get_running_app()
        tracker = getattr(app, "tracker", None) if app is not None else None
        if tracker is None:
            return

        def work():
            try:
                return tracker.get_app_icon_path(pkg)
            except Exception:
                return ""
            finally:
                detach_current_thread()

        future = _ICON_POOL.submit(work)

        def done(fut):
            try:
                path = fut.result() or ""
            except Exception:
                return
            Clock.schedule_once(lambda dt: self._set_icon(pkg, path), 0)

        future.add_done_callback(done)

    def _set_icon(self, pkg: str, path: str):
        if pkg == self.pkg:
            self.icon_path = path


def _shorten(text, limit: int = 34) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tint(color: str, alpha: float = 0.16):
    r, g, b, _a = THEME[color]
    return (r, g, b, alpha)


def _wrap_label(text="", size="12.5sp", color="muted", bold=False, markup=False):
    lbl = Factory.WrapLabel()
    lbl.text = ar(text)
    lbl.font_size = size
    lbl.bold = bold
    lbl.markup = markup
    lbl.theme_text_color = "Custom"
    lbl.text_color = THEME[color]
    return lbl


def _rtl_label(text="", size="13sp", color="text", bold=False, height=24):
    lbl = Factory.RtlLabel()
    lbl.text = ar(text)
    lbl.font_size = size
    lbl.bold = bold
    lbl.height = dp(height)
    lbl.theme_text_color = "Custom"
    lbl.text_color = THEME[color]
    return lbl


def _chip(text: str, color: str, width: int = 84, height: int = 26, font_size: str = "11sp"):
    card = MDCard(
        md_bg_color=THEME[color],
        radius=[dp(13), dp(13), dp(13), dp(13)],
        padding=0,
        size_hint=(None, None),
        size=(dp(width), dp(height)),
    )
    card.add_widget(
        MDLabel(
            text=ar(text),
            font_name=ARABIC_FONT,
            font_size=font_size,
            bold=True,
            halign="center",
            valign="middle",
            theme_text_color="Custom",
            text_color=THEME["bg_primary"],
        )
    )
    return card


def _icon_badge(icon: str, color: str, size: int = 40):
    card = MDCard(
        md_bg_color=_tint(color),
        radius=[dp(size * 0.34)] * 4,
        padding=0,
        size_hint=(None, None),
        size=(dp(size), dp(size)),
    )
    card.add_widget(
        MDIcon(
            icon=icon,
            font_size=f"{int(size * 0.55)}sp",
            halign="center",
            valign="middle",
            theme_icon_color="Custom",
            icon_color=THEME[color],
        )
    )
    return card


def _rounded_button(
    text: str,
    bg: str,
    fg: str,
    on_release,
    height: int = 44,
    font_size: str = "13sp",
    bold: bool = False,
    width=None,
):
    btn = MDFillRoundFlatButton(
        text=ar(text),
        font_name=ARABIC_FONT,
        font_size=font_size,
        md_bg_color=THEME[bg],
        text_color=THEME[fg],
        size_hint=(None if width else 1, None),
        height=dp(height),
    )
    if width:
        btn.width = dp(width)
    btn.bind(on_release=lambda *a: on_release())
    return btn


KV = f"""
#:import get_color_from_hex kivy.utils.get_color_from_hex

<WrapLabel@MDLabel>:
    font_name: "{ARABIC_FONT}"
    line_height: 1.3
    size_hint_y: None
    text_size: self.width, None
    height: self.texture_size[1]
    valign: "top"
    halign: "right"

<RtlLabel@MDLabel>:
    font_name: "{ARABIC_FONT}"
    line_height: 1.3
    size_hint_y: None
    height: dp(24)
    halign: "right"
    text_size: self.width, None
    valign: "middle"

<Card@MDCard>:
    md_bg_color: get_color_from_hex("{THEME_HEX['card']}")
    line_color: get_color_from_hex("{THEME_HEX['border']}")
    line_width: dp(1)
    radius: [dp(22), dp(22), dp(22), dp(22)]
    padding: dp(16)
    spacing: dp(10)
    size_hint_y: None
    height: self.minimum_height

<SectionCaption@MDLabel>:
    font_name: "{ARABIC_FONT}"
    font_size: "11sp"
    bold: True
    size_hint_y: None
    height: dp(18)
    halign: "right"
    text_size: self.width, None
    theme_text_color: "Custom"
    text_color: get_color_from_hex("{THEME_HEX['muted']}")

<AppPickRow>:
    orientation: "horizontal"
    spacing: dp(10)
    padding: [dp(8), dp(5)]
    size_hint_y: None
    height: dp(62)
    canvas.before:
        Color:
            rgba: (1, 0.48, 0.24, 0.16) if root.selected else (0, 0, 0, 0)
        RoundedRectangle:
            pos: self.pos
            size: self.size
            radius: [dp(14), dp(14), dp(14), dp(14)]
    MDCheckbox:
        id: cb
        active: root.selected
        on_active: root.on_checkbox(self.active)
        size_hint: None, None
        size: dp(32), dp(32)
        pos_hint: {{"center_y": 0.5}}
    MDBoxLayout:
        orientation: "vertical"
        spacing: dp(1)
        WrapLabel:
            text: root.app_name
            font_size: "13.5sp"
            bold: True
            text_color: get_color_from_hex("{THEME_HEX['text']}")
        MDLabel:
            text: root.pkg
            font_name: "{ARABIC_FONT}"
            font_size: "10.5sp"
            halign: "right"
            text_size: self.width, None
            theme_text_color: "Custom"
            text_color: get_color_from_hex("{THEME_HEX['muted']}")
            size_hint_y: None
            height: dp(15)
    Image:
        source: root.icon_path
        opacity: 1 if root.icon_path else 0
        size_hint: None, None
        size: dp(30), dp(30)
        pos_hint: {{"center_y": 0.5}}

<OnboardingScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
    canvas.before:
        Color:
            rgba: 1, 0.48, 0.10, 0.07
        Ellipse:
            pos: self.width * 0.52, self.height * 0.80
            size: dp(340), dp(340)
        Color:
            rgba: 0.13, 0.83, 0.93, 0.05
        Ellipse:
            pos: -dp(90), self.height * 0.12
            size: dp(280), dp(280)
    MDBoxLayout:
        orientation: "vertical"
        padding: [dp(16), dp(20), dp(16), dp(12)]
        spacing: dp(12)

        MDBoxLayout:
            size_hint_y: None
            height: dp(62)
            spacing: dp(12)

            MDCard:
                size_hint: None, None
                size: dp(54), dp(54)
                radius: [dp(18), dp(18), dp(18), dp(18)]
                md_bg_color: 1, 0.48, 0.10, 0.18
                padding: 0
                MDIcon:
                    icon: "shield-star-outline"
                    font_size: "27sp"
                    theme_icon_color: "Custom"
                    icon_color: get_color_from_hex("{THEME_HEX['accent']}")

            MDBoxLayout:
                orientation: "vertical"
                spacing: dp(1)
                MDLabel:
                    text: "Motiv_Force"
                    font_name: "{ARABIC_FONT}"
                    font_size: "23sp"
                    bold: True
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['text']}")
                MDLabel:
                    text: app.ar("حماية ذكية من التشتت، بخطوات بسيطة")
                    font_name: "{ARABIC_FONT}"
                    font_size: "11.5sp"
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['muted']}")

        Card:
            id: perm_summary
            orientation: "vertical"
            padding: dp(14)
            spacing: dp(8)

            MDBoxLayout:
                size_hint_y: None
                height: dp(24)
                MDLabel:
                    id: lbl_perm_count
                    text: "0 / 4"
                    font_name: "{ARABIC_FONT}"
                    font_size: "12sp"
                    bold: True
                    halign: "right"
                    size_hint_x: None
                    width: dp(96)
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['accent_soft']}")
                RtlLabel:
                    id: lbl_title
                    text: ""
                    font_size: "15sp"
                    bold: True
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['text']}")

            MDProgressBar:
                id: perm_progress
                value: 0
                size_hint_y: None
                height: dp(8)
                color: get_color_from_hex("{THEME_HEX['accent']}")

            WrapLabel:
                id: lbl_sub
                text: ""
                font_size: "12sp"
                theme_text_color: "Custom"
                text_color: get_color_from_hex("{THEME_HEX['muted']}")

        ScrollView:
            bar_width: dp(2)
            scroll_type: ["bars", "content"]
            MDBoxLayout:
                id: perm_list
                orientation: "vertical"
                spacing: dp(10)
                size_hint_y: None
                height: self.minimum_height
                padding: [0, dp(2), 0, dp(2)]

        MDFillRoundFlatButton:
            id: btn_continue
            text: ""
            font_name: "{ARABIC_FONT}"
            font_size: "15sp"
            md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
            text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
            disabled: True
            size_hint_x: 1
            size_hint_y: None
            height: dp(52)
            on_release: root.enter_dashboard()

<DashboardScreen>:
    md_bg_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
    canvas.before:
        Color:
            rgba: 0.13, 0.83, 0.93, 0.05
        Ellipse:
            pos: self.width * 0.62, self.height * 0.86
            size: dp(280), dp(280)
    MDBoxLayout:
        orientation: "vertical"

        MDBoxLayout:
            size_hint_y: None
            height: dp(82)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(14), dp(12)]
            spacing: dp(8)

            MDIconButton:
                icon: "cog-outline"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['accent']}")
                pos_hint: {{"center_y": 0.5}}
                on_release: app.go_screen("settings")

            MDBoxLayout:
                orientation: "vertical"
                spacing: dp(1)
                MDLabel:
                    id: dash_header
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    font_size: "20sp"
                    bold: True
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['text']}")
                MDLabel:
                    id: dash_sub
                    text: app.ar("Motiv_Force · حماية ذكية")
                    font_name: "{ARABIC_FONT}"
                    font_size: "11sp"
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['muted']}")

        ScrollView:
            bar_width: dp(2)
            scroll_type: ["bars", "content"]
            MDBoxLayout:
                orientation: "vertical"
                padding: [dp(14), dp(14), dp(14), dp(28)]
                spacing: dp(12)
                size_hint_y: None
                height: self.minimum_height

                Card:
                    id: lock_card
                    orientation: "vertical"

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(40)
                        spacing: dp(10)

                        MDCard:
                            size_hint: None, None
                            size: dp(40), dp(40)
                            radius: [dp(14), dp(14), dp(14), dp(14)]
                            md_bg_color: 0.96, 0.62, 0.04, 0.16
                            padding: 0
                            MDIcon:
                                icon: "shield-alert-outline"
                                font_size: "22sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['warning']}")

                        MDLabel:
                            id: lock_badge
                            text: ""
                            font_name: "{ARABIC_FONT}"
                            bold: True
                            font_size: "15sp"
                            halign: "right"
                            text_size: self.width, None
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['warning']}")

                    MDLabel:
                        id: lock_timer
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        bold: True
                        font_size: "30sp"
                        halign: "right"
                        text_size: self.width, None
                        size_hint_y: None
                        height: dp(38)
                        opacity: 0
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['cyan']}")

                    WrapLabel:
                        id: lock_desc
                        text: ""
                        font_size: "12.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")

                    MDFillRoundFlatButton:
                        id: btn_lock_action
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(46)
                        on_release: root.on_lock_action()

                MDBoxLayout:
                    size_hint_y: None
                    height: dp(88)
                    spacing: dp(10)

                    Card:
                        orientation: "vertical"
                        padding: dp(14)
                        spacing: dp(2)
                        MDLabel:
                            id: stat_rules
                            text: "0"
                            font_name: "{ARABIC_FONT}"
                            font_size: "26sp"
                            bold: True
                            halign: "right"
                            text_size: self.width, None
                            size_hint_y: None
                            height: dp(34)
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['accent']}")
                        MDLabel:
                            text: app.ar("قاعدة نشطة")
                            font_name: "{ARABIC_FONT}"
                            font_size: "11sp"
                            halign: "right"
                            text_size: self.width, None
                            size_hint_y: None
                            height: dp(16)
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['muted']}")

                    Card:
                        orientation: "vertical"
                        padding: dp(14)
                        spacing: dp(2)
                        MDLabel:
                            id: stat_blocked
                            text: "0"
                            font_name: "{ARABIC_FONT}"
                            font_size: "26sp"
                            bold: True
                            halign: "right"
                            text_size: self.width, None
                            size_hint_y: None
                            height: dp(34)
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        MDLabel:
                            text: app.ar("تطبيق محظور الآن")
                            font_name: "{ARABIC_FONT}"
                            font_size: "11sp"
                            halign: "right"
                            text_size: self.width, None
                            size_hint_y: None
                            height: dp(16)
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['muted']}")

                MDBoxLayout:
                    size_hint_y: None
                    height: dp(46)
                    spacing: dp(8)

                    MDFillRoundFlatButton:
                        id: btn_add_rule
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint: None, None
                        size: dp(138), dp(40)
                        pos_hint: {{"center_y": 0.5}}
                        on_release: root.open_new_rule()

                    MDLabel:
                        id: rules_section_lbl
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "17sp"
                        bold: True
                        halign: "right"
                        text_size: self.width, None
                        valign: "middle"
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
            height: dp(74)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(14), dp(10)]
            spacing: dp(8)

            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['text']}")
                pos_hint: {{"center_y": 0.5}}
                on_release: app.go_screen("dashboard")

            MDBoxLayout:
                orientation: "vertical"
                spacing: dp(1)
                MDLabel:
                    id: rb_title
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    font_size: "18sp"
                    bold: True
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['text']}")
                MDLabel:
                    text: app.ar("اختر التطبيقات والشرط، ثم فعّل القاعدة")
                    font_name: "{ARABIC_FONT}"
                    font_size: "10.5sp"
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['muted']}")

        ScrollView:
            bar_width: dp(2)
            scroll_type: ["bars", "content"]
            MDBoxLayout:
                orientation: "vertical"
                padding: [dp(14), dp(12), dp(14), dp(24)]
                spacing: dp(12)
                size_hint_y: None
                height: self.minimum_height

                Card:
                    orientation: "vertical"

                    SectionCaption:
                        text: "اسم القاعدة"
                    MDTextField:
                        id: input_rule_name
                        font_name: "{ARABIC_FONT}"
                        hint_text: app.ar("مثال: صباح بلا سوشال ميديا")
                        mode: "rectangle"
                        size_hint_y: None
                        height: dp(54)

                Card:
                    orientation: "vertical"

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(40)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(38), dp(38)
                            radius: [dp(13), dp(13), dp(13), dp(13)]
                            md_bg_color: 0.96, 0.62, 0.04, 0.16
                            padding: 0
                            MDIcon:
                                icon: "cellphone-lock"
                                font_size: "21sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['warning']}")
                        RtlLabel:
                            id: lbl_target_sec
                            text: ""
                            font_size: "14.5sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['warning']}")

                    WrapLabel:
                        id: lbl_target_summary
                        text: ""
                        font_size: "12.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")

                    MDFillRoundFlatButton:
                        id: btn_pick_targets
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(44)
                        on_release: root.open_app_picker("target")

                Card:
                    orientation: "vertical"

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(40)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(38), dp(38)
                            radius: [dp(13), dp(13), dp(13), dp(13)]
                            md_bg_color: 0.13, 0.83, 0.93, 0.14
                            padding: 0
                            MDIcon:
                                icon: "check-decagram-outline"
                                font_size: "21sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        RtlLabel:
                            id: lbl_source_sec
                            text: ""
                            font_size: "14.5sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['cyan']}")

                    WrapLabel:
                        id: lbl_source_summary
                        text: ""
                        font_size: "12.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")

                    MDFillRoundFlatButton:
                        id: btn_pick_sources
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "13sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(44)
                        on_release: root.open_app_picker("source")

                Card:
                    orientation: "vertical"

                    SectionCaption:
                        text: app.ar("الوقت المطلوب")
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(56)
                        spacing: dp(10)
                        MDTextField:
                            id: input_minutes
                            font_name: "{ARABIC_FONT}"
                            input_filter: "int"
                            text: "20"
                            mode: "rectangle"
                            size_hint_x: None
                            width: dp(120)
                        MDLabel:
                            text: app.ar("دقيقة من تطبيقات المصدر")
                            font_name: "{ARABIC_FONT}"
                            font_size: "12.5sp"
                            halign: "right"
                            text_size: self.width, None
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['muted']}")

                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(46)
                        spacing: dp(10)
                        MDSwitch:
                            id: switch_mode
                            active: False
                            pos_hint: {{"center_y": 0.5}}
                            on_active: root.on_mode_switch(self.active)
                        MDLabel:
                            id: lbl_mode_title
                            text: ""
                            font_name: "{ARABIC_FONT}"
                            font_size: "13sp"
                            bold: True
                            halign: "right"
                            text_size: self.width, None
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")

                    WrapLabel:
                        id: lbl_mode_desc
                        text: ""
                        font_size: "12sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")

                MDFillRoundFlatButton:
                    id: btn_save_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    font_size: "15sp"
                    md_bg_color: get_color_from_hex("{THEME_HEX['accent']}")
                    text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                    size_hint_x: 1
                    size_hint_y: None
                    height: dp(52)
                    on_release: root.save_rule()

                MDFlatButton:
                    id: btn_delete_rule
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    font_size: "13sp"
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['danger']}")
                    size_hint_x: 1
                    size_hint_y: None
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
            height: dp(74)
            md_bg_color: get_color_from_hex("{THEME_HEX['bg_secondary']}")
            padding: [dp(14), dp(10)]
            spacing: dp(8)

            MDIconButton:
                icon: "arrow-left"
                theme_icon_color: "Custom"
                icon_color: get_color_from_hex("{THEME_HEX['text']}")
                pos_hint: {{"center_y": 0.5}}
                on_release: app.go_screen("dashboard")

            MDBoxLayout:
                orientation: "vertical"
                spacing: dp(1)
                MDLabel:
                    id: settings_title
                    text: ""
                    font_name: "{ARABIC_FONT}"
                    font_size: "18sp"
                    bold: True
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['text']}")
                MDLabel:
                    text: app.ar("التحكم الكامل بإعدادات الحماية")
                    font_name: "{ARABIC_FONT}"
                    font_size: "10.5sp"
                    halign: "right"
                    text_size: self.width, None
                    theme_text_color: "Custom"
                    text_color: get_color_from_hex("{THEME_HEX['muted']}")

        ScrollView:
            bar_width: dp(2)
            scroll_type: ["bars", "content"]
            MDBoxLayout:
                orientation: "vertical"
                padding: [dp(14), dp(12), dp(14), dp(24)]
                spacing: dp(12)
                size_hint_y: None
                height: self.minimum_height

                Card:
                    orientation: "vertical"
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(38)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(36), dp(36)
                            radius: [dp(12), dp(12), dp(12), dp(12)]
                            md_bg_color: 0.13, 0.83, 0.93, 0.14
                            padding: 0
                            MDIcon:
                                icon: "clock-outline"
                                font_size: "20sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        RtlLabel:
                            id: lbl_reset_title
                            text: ""
                            font_size: "14sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")
                    WrapLabel:
                        id: lbl_reset_val
                        text: ""
                        font_size: "12.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                    MDFillRoundFlatButton:
                        id: btn_pick_reset_time
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12.5sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['cyan']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(44)
                        on_release: root.open_time_picker()

                Card:
                    orientation: "vertical"
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(38)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(36), dp(36)
                            radius: [dp(12), dp(12), dp(12), dp(12)]
                            md_bg_color: 0.96, 0.62, 0.04, 0.16
                            padding: 0
                            MDIcon:
                                icon: "lock-clock"
                                font_size: "20sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['warning']}")
                        RtlLabel:
                            id: lbl_strict_title
                            text: ""
                            font_size: "14sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")
                    WrapLabel:
                        id: lbl_strict_status
                        text: ""
                        font_size: "12.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
                    MDFillRoundFlatButton:
                        id: btn_strict_unlock
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12.5sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['warning']}")
                        text_color: get_color_from_hex("{THEME_HEX['bg_primary']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(44)
                        on_release: root.handle_strict_button()

                Card:
                    orientation: "vertical"
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(38)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(36), dp(36)
                            radius: [dp(12), dp(12), dp(12), dp(12)]
                            md_bg_color: 1, 0.48, 0.10, 0.16
                            padding: 0
                            MDIcon:
                                icon: "shield-account-outline"
                                font_size: "20sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['accent']}")
                        RtlLabel:
                            id: lbl_perms_link
                            text: ""
                            font_size: "14sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")
                    MDFillRoundFlatButton:
                        id: btn_recheck_perms
                        text: ""
                        font_name: "{ARABIC_FONT}"
                        font_size: "12.5sp"
                        md_bg_color: get_color_from_hex("{THEME_HEX['card_hover']}")
                        text_color: get_color_from_hex("{THEME_HEX['accent_soft']}")
                        size_hint_x: 1
                        size_hint_y: None
                        height: dp(44)
                        on_release: app.go_screen("onboarding")

                Card:
                    orientation: "vertical"
                    MDBoxLayout:
                        size_hint_y: None
                        height: dp(38)
                        spacing: dp(10)
                        MDCard:
                            size_hint: None, None
                            size: dp(36), dp(36)
                            radius: [dp(12), dp(12), dp(12), dp(12)]
                            md_bg_color: 0.96, 0.25, 0.37, 0.16
                            padding: 0
                            MDIcon:
                                icon: "alert-octagon-outline"
                                font_size: "20sp"
                                theme_icon_color: "Custom"
                                icon_color: get_color_from_hex("{THEME_HEX['danger']}")
                        RtlLabel:
                            id: lbl_tamper_title
                            text: ""
                            font_size: "14sp"
                            bold: True
                            theme_text_color: "Custom"
                            text_color: get_color_from_hex("{THEME_HEX['text']}")
                    WrapLabel:
                        id: lbl_tamper_logs
                        text: ""
                        font_size: "11.5sp"
                        theme_text_color: "Custom"
                        text_color: get_color_from_hex("{THEME_HEX['muted']}")
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
                # نحتفظ بمرجع بايثون للكائن حتى ينفذه Java، وإلا يجمعه GC
                # فينهار التطبيق عند أول Toast.
                runnable = _ToastRunnable(str(msg))
                _TOAST_REFS.append(runnable)
                act.runOnUiThread(runnable)
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
            if mode == AppOpsManager.MODE_ALLOWED:
                return True
            # بعض أجهزة OEM ترجع MODE_DEFAULT رغم أن المستخدم منح الصلاحية،
            # فيظل التطبيق يطلبها للأبد. نتحقق هنا من وجود بيانات استخدام فعلية.
            if mode == AppOpsManager.MODE_DEFAULT:
                try:
                    usm = act.getSystemService(Context.USAGE_STATS_SERVICE)
                    now_ms = int(System.currentTimeMillis())
                    stats = usm.queryUsageStats(
                        UsageStatsManager.INTERVAL_DAILY, now_ms - 3600000, now_ms
                    )
                    return stats is not None and stats.size() > 0
                except Exception:
                    return False
            return False
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
    _cards = None
    _completed = False

    @staticmethod
    def _items(state):
        return [
            (
                "chart-timeline-variant",
                "الوصول لبيانات الاستخدام",
                "لقراءة دقائق استخدام التطبيقات بدقة يومياً.",
                state[0],
                PermissionBridge.open_usage_settings,
            ),
            (
                "layers-outline",
                "الظهور فوق التطبيقات",
                "لعرض شاشة الحظر الفورية عند فتح تطبيق مقيد.",
                state[1],
                PermissionBridge.open_overlay_settings,
            ),
            (
                "gesture-tap",
                "خدمة إمكانية الوصول",
                "لمراقبة التطبيق المفتوح ومنع التلاعب أثناء القفل.",
                state[2],
                PermissionBridge.open_accessibility_settings,
            ),
            (
                "shield-lock-outline",
                "مشرف الجهاز",
                "لمنع إلغاء تثبيت التطبيق أثناء القفل الصارم.",
                state[3],
                PermissionBridge.request_device_admin,
            ),
        ]

    def on_pre_enter(self, *args):
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
        self.ids.lbl_title.text = ar("تهيئة الصلاحيات الأساسية")
        self.ids.lbl_sub.text = ar(
            "فعّل الصلاحيات الأربع التالية مرة واحدة، وسنعتني بالباقي:"
        )
        self.ids.btn_continue.text = ar("متابعة إلى لوحة التحكم")
        self._last_perm_state = None
        try:
            self._ensure_cards()
        except Exception:
            traceback.print_exc()
        self.refresh_permissions(force=True)
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_permissions(), 2.0)

    def on_pre_leave(self, *args):
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
            self._ev = None

    def _ensure_cards(self):
        """تُبنى البطاقات مرة واحدة فقط، ثم نحدّث نصوصها بدل إعادة بنائها."""
        if self._cards:
            return
        self._cards = []
        for icon, title, desc, _granted, action in self._items((False,) * 4):
            card = MDCard(
                md_bg_color=THEME["card"],
                line_color=THEME["border"],
                line_width=dp(1),
                radius=[dp(20)] * 4,
                padding=dp(14),
                spacing=dp(10),
                orientation="vertical",
                size_hint_y=None,
                height=dp(10),
            )
            card.bind(minimum_height=card.setter("height"))

            top = MDBoxLayout(size_hint_y=None, height=dp(42), spacing=dp(10))
            top.add_widget(_icon_badge(icon, "accent", 38))
            col = MDBoxLayout(orientation="vertical", spacing=dp(1))
            col.add_widget(_rtl_label(title, "14sp", "text", True, 22))
            col.add_widget(_wrap_label(desc, "11.5sp", "muted"))
            top.add_widget(col)

            bottom = MDBoxLayout(size_hint_y=None, height=dp(36), spacing=dp(8))
            status = _chip("غير مفعلة", "warning", 88, 24, "10.5sp")
            status_lbl = status.children[0]
            btn = _rounded_button(
                "تفعيل الآن",
                "accent",
                "bg_primary",
                (lambda cb=action: self._run_action(cb)),
                36,
                "11.5sp",
                True,
                140,
            )
            bottom.add_widget(btn)
            bottom.add_widget(MDBoxLayout())
            bottom.add_widget(status)

            card.add_widget(top)
            card.add_widget(bottom)
            self.ids.perm_list.add_widget(card)
            self._cards.append(
                {"card": card, "status": status, "status_lbl": status_lbl, "btn": btn}
            )

    def _run_action(self, action):
        try:
            action()
        except Exception:
            traceback.print_exc()
            notify_snack("تعذر فتح إعدادات الصلاحية. حاول مرة أخرى.")

    def refresh_permissions(self, force: bool = False):
        app = MDApp.get_running_app()
        if app is not None and getattr(app, "_paused", False):
            return
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
            if not self._cards:
                self._ensure_cards()

            for entry, granted in zip(self._cards, state_tuple):
                entry["status_lbl"].text = ar("مفعلة" if granted else "غير مفعلة")
                entry["status"].md_bg_color = (
                    THEME["success"] if granted else THEME["warning"]
                )
                btn = entry["btn"]
                btn.text = ar("تم التفعيل" if granted else "تفعيل الآن")
                btn.md_bg_color = (
                    THEME["card_hover"] if granted else THEME["accent"]
                )
                btn.text_color = (
                    THEME["success"] if granted else THEME["bg_primary"]
                )
                btn.disabled = granted

            done = sum(1 for s in state_tuple if s)
            self.ids.perm_progress.value = done * 25
            self.ids.lbl_perm_count.text = ar(f"{done} / 4 مكتملة")

            all_ok = all(state_tuple)
            self.ids.btn_continue.disabled = not all_ok
            if all_ok and not getattr(self, "_was_all_ok", False):
                if not app.db.is_onboarding_complete():
                    app.db.set_onboarding_complete(True)
                app.sync_service_state()
                Clock.schedule_once(lambda dt: app.start_background_service(), 0.2)
            self._was_all_ok = all_ok
        except Exception:
            traceback.print_exc()

    def enter_dashboard(self):
        app = MDApp.get_running_app()
        app.db.set_onboarding_complete(True)
        app.start_background_service()
        app.go_screen("dashboard")


class DashboardScreen(MDScreen):
    _rule_widgets: dict = {}
    _busy = False
    _gen = 0
    _label_cache: dict[str, str] = {}

    def on_pre_enter(self, *args):
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
        self.ids.dash_header.text = ar("لوحة التحكم")
        self.ids.btn_add_rule.text = ar("+ قاعدة جديدة")
        self.refresh_dashboard(force=True)
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_dashboard(), 3.5)

    def on_pre_leave(self, *args):
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
            self._ev = None

    def _new_card(self):
        card = MDCard(
            md_bg_color=THEME["card"],
            line_color=THEME["border"],
            line_width=dp(1),
            radius=[dp(22)] * 4,
            padding=dp(16),
            spacing=dp(10),
            orientation="vertical",
            size_hint_y=None,
            height=dp(10),
        )
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

        كل ما يلمس Java أو UsageStats أو قاعدة البيانات يجري في خيط منفصل،
        ولا نلمس أي ويدجت قبل العودة إلى خيط الواجهة.
        """
        app = MDApp.get_running_app()
        if app is None or getattr(app, "_paused", False):
            return
        if self._busy:
            if force:
                self._force_again = True
            return
        self._busy = True
        self._force_again = False
        self._gen += 1
        gen = self._gen

        def worker():
            err = ""
            evaluations, blocked_targets, usage = [], {}, {}
            lock_info = {}
            try:
                now_elapsed, now_wall = get_android_clocks()
                lock_info = app.db.sync_monotonic_clock(now_elapsed, now_wall)
                evaluations, blocked_targets, usage = app.tracker.evaluate_all_rules(app.db)
            except Exception:
                err = traceback.format_exc()
                print(f"[DASHBOARD] evaluate failed:\n{err}")
            finally:
                detach_current_thread()
            Clock.schedule_once(
                lambda dt: self._apply(
                    gen, evaluations, blocked_targets, usage, err, lock_info
                ),
                0,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _apply(self, gen, evaluations, blocked_targets, usage, err, lock_info):
        if gen != self._gen:
            return
        self._busy = False
        app = MDApp.get_running_app()
        if app is not None and getattr(app, "_paused", False):
            self._force_again = False
            return
        try:
            app = MDApp.get_running_app()
            if lock_info:
                self._render_lock(lock_info)
            self.ids.stat_rules.text = str(
                sum(1 for ev in evaluations if ev["rule"]["enabled"])
            )
            self.ids.stat_blocked.text = str(len(blocked_targets))
            self.ids.rules_section_lbl.text = ar(
                f"القواعد النشطة · {len(evaluations)}"
            )

            # لا تُعد بناء البطاقات إن لم يتغير هيكل البيانات؛ أرقام التقدم
            # فقط تُحدَّث داخل البطاقات القائمة (إعادة البناء كل 3.5 ثانية
            # كانت تسبب تقطعاً واضحاً عند وجود عدة قواعد).
            state_key = lock_info.get("state") if lock_info else ""
            signature = (
                state_key,
                bool(err),
                tuple(
                    (
                        ev["rule"]["id"],
                        ev["rule"]["name"],
                        ev["rule"]["enabled"],
                        ev["is_unlocked"],
                        tuple(ev["rule"]["target_packages"]),
                        tuple(ev["rule"]["source_packages"]),
                        ev["rule"]["mode"],
                        ev["rule"]["required_minutes"],
                    )
                    for ev in evaluations
                ),
            )
            if signature != getattr(self, "_rules_signature", None):
                self._rules_signature = signature
                self._render_rules(evaluations, err, usage)
            else:
                self._update_rule_progress(evaluations)
            try:
                app.db.export_blocker_state(
                    app.state_json_path, list(blocked_targets.keys())
                )
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
        if getattr(self, "_force_again", False):
            self._force_again = False
            Clock.schedule_once(lambda dt: self.refresh_dashboard(), 0.1)

    def _render_lock(self, lock_info):
        state = lock_info["state"]
        rem = lock_info["remaining_seconds"]
        timer = self.ids.lock_timer
        btn = self.ids.btn_lock_action

        if state == LOCK_STATE_LOCKED:
            self.ids.lock_badge.text = ar("الحماية الصارمة مفعّلة")
            self.ids.lock_badge.text_color = THEME["warning"]
            timer.opacity = 0
            timer.height = 0
            timer.text = ""
            self.ids.lock_desc.text = ar(
                f"لا يمكن حذف القواعد أو تعديلها. لفتح نافذة تعديل {GR_LABEL} ابدأ عداد {CD_LABEL}."
            )
            btn.text = ar(f"طلب فتح نافذة تعديل ({CD_LABEL})")
            btn.md_bg_color = THEME["accent"]
            btn.text_color = THEME["bg_primary"]
        elif state == LOCK_STATE_WAITING_24H:
            self.ids.lock_badge.text = ar("بانتظار انتهاء العداد")
            self.ids.lock_badge.text_color = THEME["cyan"]
            timer.opacity = 1
            timer.height = dp(38)
            timer.text = format_hhmmss(rem)
            timer.text_color = THEME["cyan"]
            self.ids.lock_desc.text = ar(
                f"القيود كلها مفعّلة حتى ينتهي العد، ثم تُفتح نافذة تعديل {GR_LABEL} فقط."
            )
            btn.text = ar("إلغاء الطلب والعودة للقفل الفوري")
            btn.md_bg_color = THEME["danger"]
            btn.text_color = THEME["text"]
        else:
            self.ids.lock_badge.text = ar("نافذة التعديل مفتوحة")
            self.ids.lock_badge.text_color = THEME["success"]
            timer.opacity = 1
            timer.height = dp(38)
            timer.text = format_hhmmss(rem)
            timer.text_color = THEME["success"]
            self.ids.lock_desc.text = ar(
                "يمكنك الآن تعديل القواعد أو حذفها قبل انتهاء المهلة."
            )
            btn.text = ar("إعادة القفل الصارم الآن")
            btn.md_bg_color = THEME["cyan"]
            btn.text_color = THEME["bg_primary"]

    def _render_rules(self, evaluations, err, usage):
        container = self.ids.rules_container
        container.clear_widgets()
        self._rule_widgets = {}

        try:
            if not PermissionBridge.all_granted():
                warn = self._new_card()
                head = MDBoxLayout(size_hint_y=None, height=dp(38), spacing=dp(10))
                head.add_widget(_icon_badge("alert-outline", "warning", 38))
                head.add_widget(_rtl_label("الحماية غير مكتملة", "14sp", "warning", True, 22))
                warn.add_widget(head)
                warn.add_widget(
                    _wrap_label(
                        "بدون صلاحية الاستخدام + الظهور فوق التطبيقات + خدمة الوصول "
                        "لن يعمل المنع فعلياً. اضغط الزر لإكمالها.",
                        "12sp",
                        "muted",
                    )
                )
                warn.add_widget(
                    _rounded_button(
                        "إكمال الصلاحيات",
                        "warning",
                        "bg_primary",
                        lambda: MDApp.get_running_app().go_screen("onboarding"),
                        42,
                        "12.5sp",
                        True,
                    )
                )
                container.add_widget(warn)
        except Exception:
            traceback.print_exc()

        if err:
            card = self._new_card()
            card.add_widget(_rtl_label("تعذر حساب القواعد", "14sp", "danger", True, 22))
            details = _wrap_label(err[-600:], "9.5sp", "muted")
            details.halign = "left"
            card.add_widget(details)
            container.add_widget(card)
            return

        if not evaluations:
            card = self._new_card()
            badge_row = MDBoxLayout(size_hint_y=None, height=dp(54), spacing=0)
            badge_row.add_widget(MDBoxLayout())
            badge_row.add_widget(_icon_badge("shield-plus-outline", "accent", 52))
            badge_row.add_widget(MDBoxLayout())
            card.add_widget(badge_row)
            title = _rtl_label("لا توجد قواعد بعد", "16sp", "text", True, 26)
            title.halign = "center"
            card.add_widget(title)
            desc = _wrap_label(
                "أضف أول قاعدة: اختر التطبيقات التي تريد حظرها، والتطبيقات "
                "التي يجب استخدامها أولاً، ثم عدد الدقائق المطلوبة.",
                "12sp",
                "muted",
            )
            desc.halign = "center"
            card.add_widget(desc)
            card.add_widget(
                _rounded_button(
                    "+ إنشاء قاعدة الآن",
                    "accent",
                    "bg_primary",
                    lambda: self.open_new_rule(),
                    44,
                    "13sp",
                    True,
                )
            )
            container.add_widget(card)
            return

        for ev in evaluations:
            container.add_widget(self._rule_card(ev, usage))

    def _update_rule_progress(self, evaluations):
        for ev in evaluations:
            refs = self._rule_widgets.get(ev["rule"]["id"])
            if not refs:
                continue
            try:
                done = float(ev["completed_minutes"])
                need = float(ev["required_minutes"])
                prog = float(ev["overall_progress"])
                refs["stat"].text = ar(
                    f"{done:.1f} من {need:.0f} دقيقة اليوم · {int(prog * 100)}%"
                )
                refs["pbar"].value = int(prog * 100)
            except Exception:
                traceback.print_exc()

    def _rule_card(self, ev, usage):
        rule = ev["rule"]
        is_unlocked = ev["is_unlocked"]
        prog = float(ev["overall_progress"])
        mode_ar = "تجميعي SUM" if rule["mode"] == "SUM" else "فردي INDIVIDUAL"
        can_mod = MDApp.get_running_app().db.can_modify_restrictions()
        tone = "success" if is_unlocked else "danger"

        card = self._new_card()

        top = MDBoxLayout(size_hint_y=None, height=dp(44), spacing=dp(10))
        top.add_widget(
            _icon_badge(
                "shield-check-outline" if is_unlocked else "shield-alert-outline",
                tone,
                40,
            )
        )
        name_col = MDBoxLayout(orientation="vertical", spacing=dp(1))
        name_col.add_widget(_rtl_label(_shorten(rule["name"], 22), "15sp", "text", True, 22))
        name_col.add_widget(
            _rtl_label(
                f"{'مفتوح الآن' if is_unlocked else 'محظور الآن'} · {mode_ar}",
                "10.5sp",
                tone,
                False,
                16,
            )
        )
        top.add_widget(name_col)
        top.add_widget(_chip("مفتوح" if is_unlocked else "محظور", tone, 76, 26, "11sp"))

        tgt = ", ".join(self._label(p) for p in rule["target_packages"][:3])
        extra = len(rule["target_packages"]) - 3
        more = f" +{extra}" if extra > 0 else ""
        src = ", ".join(self._label(p) for p in rule["source_packages"][:3])
        meta = _wrap_label(
            ar(f"المحظور: {tgt}{more}\nالأولوية: {src}"),
            "11.5sp",
            "muted",
        )

        done = float(ev["completed_minutes"])
        need = float(ev["required_minutes"])
        stat = _wrap_label(
            f"{done:.1f} من {need:.0f} دقيقة اليوم · {int(prog * 100)}%",
            "12sp",
            "accent_soft" if not is_unlocked else "success",
            True,
        )
        pbar = MDProgressBar(
            value=int(prog * 100),
            color=THEME["success"] if is_unlocked else THEME["accent"],
            size_hint_y=None,
            height=dp(8),
        )

        actions = MDBoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        if can_mod:
            actions.add_widget(
                _rounded_button(
                    "تعديل",
                    "card_hover",
                    "accent_soft",
                    (lambda rid=rule["id"]: self.open_edit_rule(rid)),
                    38,
                    "11.5sp",
                    True,
                    96,
                )
            )
        else:
            actions.add_widget(
                _wrap_label(
                    f"التعديل يتطلب نافذة {GR_LABEL}",
                    "10.5sp",
                    "muted",
                )
            )
        actions.add_widget(MDBoxLayout())
        sw = MDSwitch(
            size_hint=(None, None),
            size=(dp(44), dp(36)),
            pos_hint={"center_y": 0.5},
        )
        sw.disabled = not can_mod
        sw.active = bool(rule["enabled"])
        sw.bind(active=lambda inst, val, rid=rule["id"]: self.on_toggle_rule(rid, val))
        actions.add_widget(sw)

        card.add_widget(top)
        card.add_widget(meta)
        card.add_widget(stat)
        card.add_widget(pbar)
        card.add_widget(actions)
        self._rule_widgets[rule["id"]] = {"stat": stat, "pbar": pbar}
        return card

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
        self.refresh_dashboard(force=True)

    def on_toggle_rule(self, rule_id: int, val: bool):
        app = MDApp.get_running_app()
        if not app.db.can_modify_restrictions():
            notify_snack("لا يمكن تعطيل القواعد أثناء القفل الصارم.")
            self.refresh_dashboard(force=True)
            return
        app.db.toggle_rule(rule_id, val)
        app.sync_service_state()
        self.refresh_dashboard(force=True)

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
        _PICKER_STATE["on_toggle"] = None
        self._picker_active = False
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
        self.ids.lbl_target_sec.text = ar("التطبيقات المحظورة")
        self.ids.btn_pick_targets.text = ar("اختيار التطبيقات المحظورة")
        self.ids.lbl_source_sec.text = ar("تطبيقات المصدر")
        self.ids.btn_pick_sources.text = ar("اختيار تطبيقات المصدر")
        self.ids.btn_save_rule.text = ar("حفظ وتفعيل القاعدة")
        self.ids.btn_delete_rule.text = ar("حذف هذه القاعدة")
        self._update_mode_texts(self.ids.switch_mode.active)
        self._preload_apps()

    def _preload_apps(self):
        """تسخين قائمة التطبيقات من النسخة المخزنة دون تجميد الواجهة."""
        app = MDApp.get_running_app()
        if getattr(self, "_preload_running", False):
            return
        self._preload_running = True

        def _worker():
            try:
                apps = app.tracker.get_installed_apps()
                if apps:
                    self._installed_apps = apps
                # تحديث القائمة من PackageManager في الخلفية (مرة واحدة لكل فتح).
                if not getattr(self, "_apps_refreshed", False):
                    fresh = app.tracker.get_installed_apps(force_refresh=True)
                    if fresh:
                        self._installed_apps = fresh
                        self._apps_refreshed = True
            except Exception:
                traceback.print_exc()
            finally:
                self._preload_running = False
                detach_current_thread()

        threading.Thread(target=_worker, daemon=True).start()

    def load_rule(self, rule_id: int | None):
        app = MDApp.get_running_app()
        self.editing_rule_id = rule_id
        if rule_id is None:
            self.ids.rb_title.text = ar("إنشاء قاعدة جديدة")
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
            self.ids.rb_title.text = ar("تعديل القاعدة")
            self.ids.input_rule_name.text = rule["name"]
            self.selected_targets = list(rule["target_packages"])
            self.selected_sources = list(rule["source_packages"])
            self.ids.input_minutes.text = str(rule["required_minutes"])
            self.ids.switch_mode.active = rule["mode"] == "INDIVIDUAL"
            self.ids.btn_delete_rule.opacity = 1
            self.ids.btn_delete_rule.disabled = False

        self._refresh_summaries()
        self._update_mode_texts(self.ids.switch_mode.active)

    def _summary_text(self, packages) -> str:
        names = [self._label(p) for p in packages]
        shown = "، ".join(names[:4])
        extra = len(names) - 4
        if extra > 0:
            shown += f" +{extra}"
        return shown

    def _refresh_summaries(self):
        if self.selected_targets:
            self.ids.lbl_target_summary.text = ar(
                f"المحدد ({len(self.selected_targets)}): {self._summary_text(self.selected_targets)}"
            )
        else:
            self.ids.lbl_target_summary.text = ar("لم يتم اختيار أي تطبيق محظور بعد.")

        if self.selected_sources:
            self.ids.lbl_source_summary.text = ar(
                f"المحدد ({len(self.selected_sources)}): {self._summary_text(self.selected_sources)}"
            )
        else:
            self.ids.lbl_source_summary.text = ar("لم يتم اختيار أي تطبيق مصدر بعد.")

    def on_mode_switch(self, active: bool):
        self._update_mode_texts(active)

    def _update_mode_texts(self, is_individual: bool):
        if is_individual:
            self.ids.lbl_mode_title.text = ar("فردي إلزامي (INDIVIDUAL)")
            self.ids.lbl_mode_desc.text = ar(
                "تُفتح التطبيقات المحظورة فقط بعد إتمام الدقائق المطلوبة في كل تطبيق مصدر على حدة."
            )
        else:
            self.ids.lbl_mode_title.text = ar("تجميعي (SUM)")
            self.ids.lbl_mode_desc.text = ar(
                "تُفتح التطبيقات المحظورة بمجرد وصول مجموع دقائق تطبيقات المصدر مجتمعة إلى الرقم المحدد."
            )

    def open_app_picker(self, role: str):
        """نافذة اختيار التطبيقات عبر RecycleView: صفوف مرئية فقط بلا تجميد.

        - القائمة تُقرأ من كاش على القرص أولاً ثم تُحدّث في الخلفية.
        - الصفوف تُنشأ وتُعاد استخدامها تلقائياً (بدل بناء مئات الويدجت).
        - الأيقونات تُستخرج فقط للتطبيقات الظاهرة.
        """
        app = MDApp.get_running_app()
        temp_selected = set(
            self.selected_targets if role == "target" else self.selected_sources
        )

        status_lbl = MDLabel(
            text=ar("جارٍ تحميل قائمة التطبيقات..."),
            font_name=ARABIC_FONT,
            font_size="11.5sp",
            halign="right",
            text_size=(None, None),
            size_hint_y=None,
            height=dp(24),
            theme_text_color="Custom",
            text_color=THEME["muted"],
        )
        search_field = MDTextField(
            hint_text=ar("بحث بالاسم أو الحزمة..."),
            font_name=ARABIC_FONT,
            mode="rectangle",
            size_hint_y=None,
            height=dp(52),
        )

        rv = RecycleView(size_hint=(1, 1))
        layout = RecycleBoxLayout(
            default_size=(None, dp(62)),
            default_size_hint=(1, None),
            size_hint_y=None,
            orientation="vertical",
            spacing=dp(4),
        )
        layout.bind(minimum_height=layout.setter("height"))
        rv.add_widget(layout)
        rv.viewclass = "AppPickRow"

        btn_all = MDFlatButton(
            text=ar("تحديد الكل"),
            font_name=ARABIC_FONT,
            font_size="10sp",
            theme_text_color="Custom",
            text_color=THEME["accent_soft"],
            size_hint=(None, None),
            size=(dp(84), dp(28)),
        )
        btn_clear = MDFlatButton(
            text=ar("مسح التحديد"),
            font_name=ARABIC_FONT,
            font_size="10sp",
            theme_text_color="Custom",
            text_color=THEME["muted"],
            size_hint=(None, None),
            size=(dp(88), dp(28)),
        )
        btn_refresh = MDFlatButton(
            text=ar("تحديث"),
            font_name=ARABIC_FONT,
            font_size="10sp",
            theme_text_color="Custom",
            text_color=THEME["cyan"],
            size_hint=(None, None),
            size=(dp(58), dp(28)),
        )
        tools = MDBoxLayout(size_hint_y=None, height=dp(28), spacing=dp(4))
        tools.add_widget(btn_all)
        tools.add_widget(btn_clear)
        tools.add_widget(btn_refresh)
        tools.add_widget(MDBoxLayout())

        content = MDBoxLayout(
            orientation="vertical",
            spacing=dp(8),
            size_hint_y=None,
            height=min(dp(540), max(dp(330), Window.height - dp(260))),
        )
        content.add_widget(_rtl_label("اختر التطبيقات", "17sp", "text", True, 28))
        content.add_widget(search_field)
        content.add_widget(status_lbl)
        content.add_widget(tools)
        content.add_widget(rv)

        state = {"apps": [], "query": "", "data": [], "data_map": {}}
        self._picker_active = True

        def update_status():
            shown = len(state["data"])
            status_lbl.text = ar(
                f"{shown} معروض · المحدد {len(temp_selected)}"
            )

        def refresh_list():
            if not self._picker_active:
                return
            query = state["query"]
            items = []
            for item in state["apps"]:
                name = item.get("name") or item.get("package_name") or ""
                pkg = item["package_name"]
                if query and query not in name.casefold() and query not in pkg.casefold():
                    continue
                items.append(
                    {
                        "pkg": pkg,
                        "app_name": ar(name),
                        "selected": pkg in temp_selected,
                        "icon_path": "",
                    }
                )
            state["data"] = items
            state["data_map"] = {d["pkg"]: d for d in items}
            rv.data = items
            update_status()

        def on_toggle(pkg: str, active: bool):
            if active:
                temp_selected.add(pkg)
            else:
                temp_selected.discard(pkg)
            # حدّث نسخة الصف داخل RecycleView نفسها حتى تبقى الحالة صحيحة
            # عند إعادة استخدام الصفوف أثناء التمرير.
            entry = state["data_map"].get(pkg)
            if entry is not None:
                entry["selected"] = active
            for item in rv.data:
                if item.get("pkg") == pkg:
                    item["selected"] = active
                    break
            update_status()

        def select_all(*args):
            for entry in state["data"]:
                temp_selected.add(entry["pkg"])
                entry["selected"] = True
            rv.refresh_from_data()
            update_status()

        def clear_all(*args):
            for entry in state["data"]:
                temp_selected.discard(entry["pkg"])
                entry["selected"] = False
            rv.refresh_from_data()
            update_status()

        btn_all.bind(on_release=select_all)
        btn_clear.bind(on_release=clear_all)
        _PICKER_STATE["on_toggle"] = on_toggle

        def apply_apps(apps):
            if not self._picker_active:
                return
            state["apps"] = list(apps or [])
            if state["apps"]:
                refresh_list()
            else:
                status_lbl.text = ar(
                    "تعذر جلب التطبيقات. تأكد من صلاحية الاستخدام ثم أعد المحاولة."
                )

        def load_apps(force: bool = False):
            if self._installed_apps:
                apply_apps(self._installed_apps)
            status_lbl.text = ar("جارٍ تحديث قائمة التطبيقات...")

            def worker():
                try:
                    apps = app.tracker.get_installed_apps(force_refresh=force)
                    if apps:
                        self._installed_apps = apps
                except Exception:
                    traceback.print_exc()
                    apps = []
                finally:
                    detach_current_thread()
                if apps:
                    Clock.schedule_once(lambda dt: apply_apps(apps), 0)
                elif not self._installed_apps:
                    Clock.schedule_once(
                        lambda dt: apply_apps([]),
                        0,
                    )

            threading.Thread(target=worker, daemon=True).start()

        def on_search(inst, txt):
            state["query"] = (txt or "").strip().casefold()
            pending = getattr(self, "_search_ev", None)
            if pending is not None:
                try:
                    pending.cancel()
                except Exception:
                    pass
            self._search_ev = Clock.schedule_once(lambda dt: refresh_list(), 0.2)

        search_field.bind(text=on_search)
        btn_refresh.bind(on_release=lambda *a: load_apps(True))

        def close_picker(*args):
            _PICKER_STATE["on_toggle"] = None
            self._picker_active = False
            if self.picker_dialog is not None:
                self.picker_dialog.dismiss()
                self.picker_dialog = None

        def on_confirm(*args):
            if role == "target":
                self.selected_targets = sorted(temp_selected)
            else:
                self.selected_sources = sorted(temp_selected)
            self._refresh_summaries()
            close_picker()

        try:
            self.picker_dialog = MDDialog(
                title="",
                type="custom",
                md_bg_color=THEME["card"],
                content_cls=content,
                buttons=[
                    MDFlatButton(
                        text=ar("إلغاء"),
                        font_name=ARABIC_FONT,
                        theme_text_color="Custom",
                        text_color=THEME["muted"],
                        on_release=close_picker,
                    ),
                    MDFillRoundFlatButton(
                        text=ar("تأكيد الاختيار"),
                        font_name=ARABIC_FONT,
                        font_size="12.5sp",
                        md_bg_color=THEME["accent"],
                        text_color=THEME["bg_primary"],
                        on_release=on_confirm,
                    ),
                ],
            )
            self.picker_dialog.open()
        except Exception:
            traceback.print_exc()
            _PICKER_STATE["on_toggle"] = None
            self._picker_active = False
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
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
        self.ids.settings_title.text = ar("الإعدادات")
        self.ids.lbl_reset_title.text = ar("التصفير اليومي")
        self.ids.btn_pick_reset_time.text = ar("تغيير وقت التصفير اليومي")
        self.ids.lbl_strict_title.text = ar(f"الحماية الصارمة ({CD_LABEL} ← {GR_LABEL})")
        self.ids.lbl_perms_link.text = ar("صلاحيات النظام")
        self.ids.btn_recheck_perms.text = ar("مراجعة شاشة الصلاحيات")
        self.ids.lbl_tamper_title.text = ar("سجل محاولات التلاعب")
        self.refresh_settings()
        self._ev = Clock.schedule_interval(lambda dt: self.refresh_settings(), 2.5)

    def on_pre_leave(self, *args):
        if getattr(self, "_ev", None) is not None:
            try:
                self._ev.cancel()
            except Exception:
                pass
            self._ev = None

    def refresh_settings(self):
        """قراءة قاعدة البيانات وحساب القفل في خيط منفصل، ثم تحديث الواجهة."""
        app = MDApp.get_running_app()
        if app is None or getattr(app, "_paused", False):
            return
        if getattr(self, "_busy", False):
            return
        self._busy = True

        def worker():
            data = {}
            err = None
            try:
                data["reset"] = app.db.get_reset_time()
                data["can_mod"] = app.db.can_modify_restrictions()
                now_elapsed, now_wall = get_android_clocks()
                data["lock"] = app.db.sync_monotonic_clock(now_elapsed, now_wall)
                data["logs"] = app.db.get_tamper_logs(limit=6)
            except Exception:
                err = traceback.format_exc()
                print(f"[SETTINGS] refresh failed:\n{err}")
            finally:
                detach_current_thread()
            Clock.schedule_once(lambda dt: self._apply_settings(data), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_settings(self, data):
        self._busy = False
        app = MDApp.get_running_app()
        if app is not None and getattr(app, "_paused", False):
            return
        try:
            if not data:
                return
            h, m = data["reset"]
            self.ids.lbl_reset_val.text = ar(
                f"يتم تصفير العدادات وإعادة القفل يومياً الساعة {h:02d}:{m:02d}"
            )
            self.ids.btn_pick_reset_time.disabled = not data["can_mod"]

            lock_info = data["lock"]
            state = lock_info["state"]
            rem = lock_info["remaining_seconds"]
            btn = self.ids.btn_strict_unlock

            if state == LOCK_STATE_LOCKED:
                self.ids.lbl_strict_status.text = ar(
                    f"التعديل والحذف مقفلان الآن. ابدأ عداد {CD_LABEL} لفتح نافذة "
                    f"تعديل {GR_LABEL}، أو استخدم زر الرجوع للخروج."
                )
                btn.text = ar(f"بدء عداد {CD_LABEL} لفك القفل")
                btn.md_bg_color = THEME["warning"]
                btn.text_color = THEME["bg_primary"]
            elif state == LOCK_STATE_WAITING_24H:
                self.ids.lbl_strict_status.text = ar(
                    f"العد التنازلي جارٍ: متبقي {format_hhmmss(rem)}"
                )
                btn.text = ar("إلغاء طلب فك القفل")
                btn.md_bg_color = THEME["danger"]
                btn.text_color = THEME["text"]
            else:
                self.ids.lbl_strict_status.text = ar(
                    f"نافذة التعديل مفتوحة الآن · متبقي {format_hhmmss(rem)}"
                )
                btn.text = ar("إغلاق النافذة وإعادة القفل فوراً")
                btn.md_bg_color = THEME["cyan"]
                btn.text_color = THEME["bg_primary"]

            logs = data["logs"]
            if not logs:
                self.ids.lbl_tamper_logs.text = ar("لا توجد محاولات تلاعب مسجلة.")
            else:
                lines = []
                for lg in logs:
                    dt_str = datetime.fromtimestamp(lg["timestamp_ms"] / 1000.0).strftime(
                        "%Y-%m-%d %H:%M"
                    )
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
                MDFillRoundFlatButton(
                    text=ar("حفظ"),
                    font_name=ARABIC_FONT,
                    font_size="12.5sp",
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
            self.title = "Motiv_Force"
            self.theme_cls.theme_style = "Dark"
            self.theme_cls.material_style = "M2"
            self.theme_cls.primary_palette = "Orange"

            if ARABIC_FONT != "Roboto":
                for style_name, style_cfg in self.theme_cls.font_styles.items():
                    if style_name != "Icon" and isinstance(style_cfg, list) and len(style_cfg) > 0:
                        style_cfg[0] = ARABIC_FONT

            if IS_ANDROID:
                try:
                    files_dir = str(PythonActivity.mActivity.getFilesDir().getAbsolutePath())
                except Exception:
                    files_dir = os.environ.get("ANDROID_PRIVATE") or os.path.expanduser("~")
            else:
                files_dir = os.path.join(os.path.expanduser("~"), ".motiv_force")

            os.makedirs(files_dir, exist_ok=True)
            self.db = CondBlockerDB(os.path.join(files_dir, "motiv_force.db"))
            self.state_json_path = os.path.join(files_dir, "blocker_state.json")
            self.tracker = UsageTracker(cache_dir=files_dir)

            Factory.register("AppPickRow", cls=AppPickRow)
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
            Clock.schedule_once(lambda dt: self._warm_app_list(), 2.5)
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

    def ar(self, text):
        return ar(text)

    def on_start(self):
        try:
            Window.softinput_mode = "below_target"
        except Exception:
            pass
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
        """حساب القواعد وكتابة ملف الحالة المشترك في خلفية، بعيداً عن الواجهة.

        هذا الاستدعاء كان ينفذ استعلام UsageStats كاملاً على خيط الواجهة عند
        العودة من شاشة الصلاحيات، فتتجمد الواجهة ثم يُغلق التطبيق.
        """
        if self.db is None or self.tracker is None:
            return
        if getattr(self, "_sync_busy", False):
            self._sync_pending = True
            return
        self._sync_busy = True
        self._sync_pending = False
        db = self.db
        tracker = self.tracker
        state_path = self.state_json_path

        def worker():
            try:
                _, blocked_targets, _ = tracker.evaluate_all_rules(db)
                db.export_blocker_state(state_path, list(blocked_targets.keys()))
            except Exception:
                traceback.print_exc()
            finally:
                detach_current_thread()
                Clock.schedule_once(lambda dt: self._finish_sync(), 0)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_sync(self):
        self._sync_busy = False
        if getattr(self, "_sync_pending", False):
            self._sync_pending = False
            self.sync_service_state()

    def _warm_app_list(self):
        """تحديث كاش أسماء التطبيقات في الخلفية استعداداً لشاشة الاختيار."""
        if not IS_ANDROID or self.tracker is None:
            return

        def worker():
            try:
                self.tracker.get_installed_apps(force_refresh=True)
            except Exception:
                traceback.print_exc()
            finally:
                detach_current_thread()

        threading.Thread(target=worker, daemon=True).start()

    def on_pause(self):
        # أوقف مؤقتات التحديث قبل أن يفقد أندرويد سياق OpenGL، حتى لا تُعاد
        # رسم العناصر على سياق ميت.
        self._paused = True
        self._cancel_screen_timers()
        return True

    def on_resume(self):
        self._paused = False
        # لا نعيد بناء الواجهة أو نلمس OpenGL أثناء استئناف النشاط مباشرة:
        # ننتظر حتى يكتمل استئناف النافذة ثم نحدّث بهدوء.
        try:
            if getattr(self, "_resume_ev", None) is not None:
                self._resume_ev.cancel()
        except Exception:
            pass
        self._resume_ev = Clock.schedule_once(self._post_resume, 0.7)

    def _post_resume(self, dt):
        if self._paused:
            return
        try:
            if IS_ANDROID and self.db is not None:
                self.sync_service_state()
                if self.root and getattr(self.root, "current", None):
                    curr = self.root.get_screen(self.root.current)
                    if hasattr(curr, "refresh_permissions"):
                        curr.refresh_permissions(force=True)
                    elif hasattr(curr, "refresh_dashboard"):
                        curr.refresh_dashboard(force=True)
                    elif hasattr(curr, "refresh_settings"):
                        curr.refresh_settings()
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
                ("refresh_dashboard", 3.5),
                ("refresh_settings", 2.5),
                ("refresh_permissions", 2.0),
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
        now = time.monotonic()
        if now - getattr(self, "_service_started_at", 0.0) < 4.0:
            return
        self._service_started_at = now
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
        try:
            if os.path.exists(path) and os.path.getsize(path) > 256 * 1024:
                os.remove(path)
        except Exception:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now().isoformat()} =====\n{tb_text}\n")
        print(f"[CRASH] written to {path}")
    except Exception:
        pass


def _install_exception_guard():
    """يمنع أي خطأ غير متوقع في ويدجت واحد من إغلاق التطبيق بالكامل.

    Kivy افتراضياً يتعامل مع أي استثناء داخل Clock أو اللمس بإيقاف التطبيق.
    هنا نسجّل الخطأ (لتشخيصه لاحقاً في crash_log.txt) ونسمح للتطبيق بالاستمرار،
    مع حد أقصى حتى لا ندخل في حلقة أخطاء لا نهائية.
    """
    try:
        from kivy.base import ExceptionHandler, ExceptionManager

        class _KeepAliveHandler(ExceptionHandler):
            def __init__(self):
                super().__init__()
                self._times = []

            def handle_exception(self, inst):
                now = time.monotonic()
                self._times = [t for t in self._times if now - t < 10.0]
                self._times.append(now)
                tb = traceback.format_exc()
                print(f"[UNCAUGHT] {tb}")
                _write_crash_log(tb)
                if len(self._times) > 40:
                    return ExceptionManager.RAISE
                return ExceptionManager.PASS

        ExceptionManager.add_handler(_KeepAliveHandler())
        print("[SAFETY] keep-alive exception handler installed")
    except Exception:
        traceback.print_exc()


if __name__ == "__main__":
    _install_exception_guard()
    try:
        CondBlockerApp().run()
    except BaseException:
        _write_crash_log(traceback.format_exc())
        raise
