import os
import time
import traceback

from database import (
    CondBlockerDB,
    LOCK_STATE_LOCKED,
    LOCK_STATE_UNLOCKED_WINDOW,
    LOCK_STATE_WAITING_24H,
)
from usage_tracker import IS_ANDROID, UsageTracker, get_android_clocks

if IS_ANDROID:
    from jnius import PythonJavaClass, autoclass, java_method

    PythonService = autoclass("org.kivy.android.PythonService")
    Context = autoclass("android.content.Context")
    Intent = autoclass("android.content.Intent")
    PowerManager = autoclass("android.os.PowerManager")
    BuildVersion = autoclass("android.os.Build$VERSION")
    ServiceInfo = autoclass("android.content.pm.ServiceInfo")
    NotificationChannel = autoclass("android.app.NotificationChannel")
    NotificationManager = autoclass("android.app.NotificationManager")
    NotificationBuilder = autoclass("android.app.Notification$Builder")
    PendingIntent = autoclass("android.app.PendingIntent")
    Settings = autoclass("android.provider.Settings")

    Looper = autoclass("android.os.Looper")
    Handler = autoclass("android.os.Handler")
    WindowManagerLayoutParams = autoclass("android.view.WindowManager$LayoutParams")
    PixelFormat = autoclass("android.graphics.PixelFormat")
    Gravity = autoclass("android.view.Gravity")
    Color = autoclass("android.graphics.Color")
    GradientDrawable = autoclass("android.graphics.drawable.GradientDrawable")
    LinearLayout = autoclass("android.widget.LinearLayout")
    LinearLayoutParams = autoclass("android.widget.LinearLayout$LayoutParams")
    ScrollView = autoclass("android.widget.ScrollView")
    TextView = autoclass("android.widget.TextView")
    Button = autoclass("android.widget.Button")
    ProgressBar = autoclass("android.widget.ProgressBar")
    AndroidR = autoclass("android.R")
    Typeface = autoclass("android.graphics.Typeface")
    ClipData = None


CHANNEL_ID = "cond_blocker_monitor_channel"
NOTIFICATION_ID = 4041


def format_hhmmss(seconds: float) -> str:
    s = max(0, int(seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


if IS_ANDROID:

    class ClickListenerProxy(PythonJavaClass):
        __javainterfaces__ = ["android/view/View$OnClickListener"]
        __javacontext__ = "app"

        def __init__(self, callback):
            super().__init__()
            self.callback = callback

        @java_method("(Landroid/view/View;)V")
        def onClick(self, view):
            try:
                self.callback()
            except Exception:
                traceback.print_exc()


    class RunnableProxy(PythonJavaClass):
        __javainterfaces__ = ["java/lang/Runnable"]
        __javacontext__ = "app"

        def __init__(self, fn, owner=None):
            super().__init__()
            self.fn = fn
            self.owner = owner

        @java_method("()V")
        def run(self):
            try:
                self.fn()
            except Exception:
                traceback.print_exc()
            finally:
                # نحرر المرجع بعد التنفيذ حتى لا يتراكم، مع إبقائه حياً
                # حتى ينفذه Java (وإلا يجمعه GC وينهار التطبيق).
                if self.owner is not None:
                    try:
                        self.owner._pending_runnables.remove(self)
                    except Exception:
                        pass


class BlockingOverlayManager:
    """
    يدير إنشاء وإزالة نافذة الحظر المنبثقة فوق التطبيقات باستخدام WindowManager
    على الخيط الرئيسي للأندرويد (Main Looper).
    """

    def __init__(self, service_ctx, tracker: UsageTracker):
        self.ctx = service_ctx
        self.tracker = tracker
        self.wm = service_ctx.getSystemService(Context.WINDOW_SERVICE) if IS_ANDROID else None
        self.main_handler = Handler(Looper.getMainLooper()) if IS_ANDROID else None
        self.overlay_view = None
        self.current_blocked_pkg = ""
        self._removing = False
        self._listeners = []
        self._progress_bars = {}
        self._progress_labels = {}
        self._last_render_ts = 0.0
        self._pending_runnables = []

    def _post(self, fn):
        if self.main_handler is None:
            return
        proxy = RunnableProxy(fn, self)
        self._pending_runnables.append(proxy)
        self.main_handler.post(proxy)

    def _dp(self, dp: int) -> int:
        density = self.ctx.getResources().getDisplayMetrics().density
        return int(dp * density)

    def _make_rounded_bg(self, hex_color: str, radius_dp: int, stroke_hex: str | None = None):
        gd = GradientDrawable()
        gd.setColor(Color.parseColor(hex_color))
        gd.setCornerRadius(float(self._dp(radius_dp)))
        if stroke_hex:
            gd.setStroke(self._dp(1), Color.parseColor(stroke_hex))
        return gd

    def show_or_update(self, blocked_pkg: str, eval_item: dict):
        if not IS_ANDROID or not Settings.canDrawOverlays(self.ctx):
            return

        now = time.monotonic()
        # إذا كانت نفس الشاشة معروضة، نحدّث أرقام التقدم كل ثانيتين دون إعادة رسم كامل
        if self.overlay_view is not None and self.current_blocked_pkg == blocked_pkg:
            if now - self._last_render_ts >= 1.5:
                self._last_render_ts = now
                self._update_progress_in_ui(eval_item)
            return

        self.current_blocked_pkg = blocked_pkg
        self._last_render_ts = now
        blocked_label = self.tracker.get_app_label(blocked_pkg)

        def _build_and_attach():
            self._remove_view_sync()
            self._listeners.clear()
            self._progress_bars.clear()
            self._progress_labels.clear()

            root = LinearLayout(self.ctx)
            root.setOrientation(LinearLayout.VERTICAL)
            root.setBackgroundColor(Color.parseColor("#F50B1120"))
            pad = self._dp(20)
            root.setPadding(pad, self._dp(38), pad, pad)
            root.setGravity(Gravity.CENTER_HORIZONTAL)

            # شارة تحذير برتقالية
            badge = TextView(self.ctx)
            badge.setText("تم حظر هذا التطبيق مؤقتاً")
            badge.setTextColor(Color.parseColor("#0B1120"))
            badge.setTextSize(13.0)
            badge.setTypeface(Typeface.DEFAULT_BOLD)
            badge.setBackground(self._make_rounded_bg("#F97316", 16))
            badge.setPadding(self._dp(14), self._dp(6), self._dp(14), self._dp(6))
            lp_badge = LinearLayoutParams(LinearLayoutParams.WRAP_CONTENT, LinearLayoutParams.WRAP_CONTENT)
            lp_badge.setMargins(0, 0, 0, self._dp(14))
            root.addView(badge, lp_badge)

            # اسم التطبيق المحظور
            title = TextView(self.ctx)
            title.setText(blocked_label)
            title.setTextColor(Color.parseColor("#F8FAFC"))
            title.setTextSize(24.0)
            title.setTypeface(Typeface.DEFAULT_BOLD)
            title.setGravity(Gravity.CENTER)
            lp_title = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, LinearLayoutParams.WRAP_CONTENT)
            lp_title.setMargins(0, 0, 0, self._dp(6))
            root.addView(title, lp_title)

            rule = eval_item["rule"]
            mode_desc = (
                "النمط التجميعي (SUM): إجمالي دقائق تطبيقات المصدر"
                if rule["mode"] == "SUM"
                else "النمط الفردي الإلزامي (INDIVIDUAL): كل تطبيق مصدر بشكل منفصل"
            )
            subtitle = TextView(self.ctx)
            subtitle.setText(f"القاعدة: {rule['name']}\n{mode_desc}")
            subtitle.setTextColor(Color.parseColor("#94A3B8"))
            subtitle.setTextSize(14.0)
            subtitle.setGravity(Gravity.CENTER)
            lp_sub = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, LinearLayoutParams.WRAP_CONTENT)
            lp_sub.setMargins(0, 0, 0, self._dp(18))
            root.addView(subtitle, lp_sub)

            # منطقة التمرير لتفاصيل تقدم كل تطبيق مصدر + أزرار الإطلاق السريع
            scroll = ScrollView(self.ctx)
            lp_scroll = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, 0, 1.0)
            cards_container = LinearLayout(self.ctx)
            cards_container.setOrientation(LinearLayout.VERTICAL)

            for item in eval_item["source_breakdown"]:
                card = LinearLayout(self.ctx)
                card.setOrientation(LinearLayout.VERTICAL)
                card.setBackground(self._make_rounded_bg("#1E293B", 14, "#334155"))
                cpad = self._dp(14)
                card.setPadding(cpad, cpad, cpad, cpad)
                lp_card = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, LinearLayoutParams.WRAP_CONTENT)
                lp_card.setMargins(0, 0, 0, self._dp(12))

                row_top = TextView(self.ctx)
                row_top.setText(item["label"])
                row_top.setTextColor(Color.parseColor("#F8FAFC"))
                row_top.setTextSize(16.0)
                row_top.setTypeface(Typeface.DEFAULT_BOLD)
                card.addView(row_top)

                prog_txt = TextView(self.ctx)
                prog_txt.setText(
                    f"المنجز: {item['used_minutes']:.1f} دقيقة / المطلوب: {item['required_minutes']} دقيقة"
                )
                prog_txt.setTextColor(Color.parseColor("#06B6D4"))
                prog_txt.setTextSize(13.0)
                lp_pt = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, LinearLayoutParams.WRAP_CONTENT)
                lp_pt.setMargins(0, self._dp(4), 0, self._dp(8))
                card.addView(prog_txt, lp_pt)

                pbar = ProgressBar(self.ctx, None, AndroidR.attr.progressBarStyleHorizontal)
                pbar.setMax(1000)
                pbar.setProgress(int(item["progress"] * 1000))
                lp_pb = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, self._dp(10))
                lp_pb.setMargins(0, 0, 0, self._dp(10))
                card.addView(pbar, lp_pb)

                spkg = item["package_name"]
                self._progress_bars[spkg] = pbar
                self._progress_labels[spkg] = prog_txt

                # زر إطلاق سريع لتطبيق المصدر
                launch_btn = Button(self.ctx)
                launch_btn.setText(f"فتح {item['label']} الآن")
                launch_btn.setTextColor(Color.parseColor("#0B1120"))
                launch_btn.setTypeface(Typeface.DEFAULT_BOLD)
                launch_btn.setBackground(self._make_rounded_bg("#06B6D4", 10))
                listener = ClickListenerProxy(lambda p=spkg: self._launch_source_app(p))
                self._listeners.append(listener)
                launch_btn.setOnClickListener(listener)
                card.addView(
                    launch_btn,
                    LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, self._dp(44)),
                )

                cards_container.addView(card, lp_card)

            scroll.addView(cards_container)
            root.addView(scroll, lp_scroll)

            # زر العودة للشاشة الرئيسية
            home_btn = Button(self.ctx)
            home_btn.setText("العودة إلى الشاشة الرئيسية")
            home_btn.setTextColor(Color.parseColor("#F8FAFC"))
            home_btn.setTypeface(Typeface.DEFAULT_BOLD)
            home_btn.setBackground(self._make_rounded_bg("#1E293B", 12, "#475569"))
            home_listener = ClickListenerProxy(self._go_home)
            self._listeners.append(home_listener)
            home_btn.setOnClickListener(home_listener)
            lp_hb = LinearLayoutParams(LinearLayoutParams.MATCH_PARENT, self._dp(48))
            lp_hb.setMargins(0, self._dp(12), 0, 0)
            root.addView(home_btn, lp_hb)

            win_type = (
                WindowManagerLayoutParams.TYPE_APPLICATION_OVERLAY
                if BuildVersion.SDK_INT >= 26
                else WindowManagerLayoutParams.TYPE_PHONE
            )
            flags = (
                WindowManagerLayoutParams.FLAG_LAYOUT_IN_SCREEN
                | WindowManagerLayoutParams.FLAG_FULLSCREEN
            )
            params = WindowManagerLayoutParams(
                WindowManagerLayoutParams.MATCH_PARENT,
                WindowManagerLayoutParams.MATCH_PARENT,
                win_type,
                flags,
                PixelFormat.TRANSLUCENT,
            )
            params.gravity = Gravity.TOP | Gravity.START
            self.wm.addView(root, params)
            self.overlay_view = root

        self._post(_build_and_attach)

    def _update_progress_in_ui(self, eval_item: dict):
        def _update():
            if self.overlay_view is None:
                return
            for item in eval_item["source_breakdown"]:
                spkg = item["package_name"]
                if spkg in self._progress_bars:
                    self._progress_bars[spkg].setProgress(int(item["progress"] * 1000))
                if spkg in self._progress_labels:
                    self._progress_labels[spkg].setText(
                        f"المنجز: {item['used_minutes']:.1f} دقيقة / المطلوب: {item['required_minutes']} دقيقة"
                    )

        self._post(_update)

    def hide(self):
        if not IS_ANDROID or self.overlay_view is None or self._removing:
            self.current_blocked_pkg = ""
            return
        self.current_blocked_pkg = ""
        # لا نكرر طلب الإزالة كل دورة (كل 0.8 ثانية) بينما هو في طريقه للتنفيذ.
        self._removing = True
        view = self.overlay_view
        self._post(lambda: self._remove_view_sync(view))

    def _remove_view_sync(self, view=None):
        if view is None:
            view = self.overlay_view
        self._removing = False
        if view is not None:
            try:
                self.wm.removeViewImmediate(view)
            except Exception:
                try:
                    self.wm.removeView(view)
                except Exception:
                    pass
            if self.overlay_view is view:
                self.overlay_view = None

    def _launch_source_app(self, pkg: str):
        try:
            pm = self.ctx.getPackageManager()
            launch_intent = pm.getLaunchIntentForPackage(pkg)
            if launch_intent is not None:
                launch_intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP)
                self._remove_view_sync()
                self.current_blocked_pkg = ""
                self.ctx.startActivity(launch_intent)
        except Exception:
            traceback.print_exc()

    def _go_home(self):
        try:
            home_intent = Intent(Intent.ACTION_MAIN)
            home_intent.addCategory(Intent.CATEGORY_HOME)
            home_intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            self._remove_view_sync()
            self.current_blocked_pkg = ""
            self.ctx.startActivity(home_intent)
        except Exception:
            traceback.print_exc()


def _create_notification(service, title: str, text: str):
    nm = service.getSystemService(Context.NOTIFICATION_SERVICE)
    if BuildVersion.SDK_INT >= 26:
        channel = NotificationChannel(
            CHANNEL_ID,
            "خدمة مراقبة Motiv_Forcer",
            NotificationManager.IMPORTANCE_LOW,
        )
        nm.createNotificationChannel(channel)
        builder = NotificationBuilder(service, CHANNEL_ID)
    else:
        builder = NotificationBuilder(service)

    pm = service.getPackageManager()
    launch_intent = pm.getLaunchIntentForPackage(service.getPackageName())
    flags = PendingIntent.FLAG_UPDATE_CURRENT
    if BuildVersion.SDK_INT >= 23:
        flags |= PendingIntent.FLAG_IMMUTABLE
    pending = PendingIntent.getActivity(service, 0, launch_intent, flags)

    builder.setContentTitle(title)
    builder.setContentText(text)
    builder.setSmallIcon(service.getApplicationInfo().icon)
    builder.setOngoing(True)
    builder.setContentIntent(pending)
    return builder.build(), nm


def _read_accessibility_fg_pkg(files_dir: str) -> str:
    fg_file = os.path.join(files_dir, "foreground_pkg.txt")
    try:
        if os.path.exists(fg_file):
            with open(fg_file, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def run_monitor_service():
    if not IS_ANDROID:
        return

    service = PythonService.mService
    if service is None:
        return
    try:
        service.setAutoRestartService(True)
    except Exception:
        pass

    files_dir = str(service.getFilesDir().getAbsolutePath())
    db_path = os.path.join(files_dir, "motiv_forcer.db")
    state_json_path = os.path.join(files_dir, "blocker_state.json")

    db = CondBlockerDB(db_path)
    tracker = UsageTracker(context=service, cache_dir=files_dir)
    overlay_mgr = BlockingOverlayManager(service, tracker)

    notif, nm = _create_notification(
        service,
        "الحظر المشروط نشط",
        "الحماية الصارمة ومراقبة الاستخدام تعمل في الخلفية",
    )
    if BuildVersion.SDK_INT >= 34:
        try:
            service.startForeground(
                NOTIFICATION_ID,
                notif,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE,
            )
        except Exception:
            traceback.print_exc()
            try:
                service.startForeground(NOTIFICATION_ID, notif)
            except Exception:
                traceback.print_exc()
    else:
        try:
            service.startForeground(NOTIFICATION_ID, notif)
        except Exception:
            traceback.print_exc()

    wake_lock = None
    try:
        pm = service.getSystemService(Context.POWER_SERVICE)
        wake_lock = pm.newWakeLock(
            PowerManager.PARTIAL_WAKE_LOCK, "Motiv_Forcer::MonitorWakeLock"
        )
        wake_lock.acquire()
    except Exception:
        traceback.print_exc()
        wake_lock = None

    my_pkg = str(service.getPackageName())
    last_notif_update = 0.0
    last_eval = -999.0
    blocked_targets: dict[str, dict] = {}
    lock_info = {"state": LOCK_STATE_LOCKED, "remaining_seconds": 0.0}
    EVAL_INTERVAL = 4.0

    try:
        while True:
            try:
                now_mono = time.monotonic()

                # تقييم الاستخدام ليس رخيصاً (قراءة أحداث UsageStats)، لذلك
                # ننفذه كل 4 ثوانٍ بدل كل نصف ثانية حتى لا تستنزف الخدمة
                # المعالج وتسبب تقطعاً في واجهة التطبيق.
                if now_mono - last_eval >= EVAL_INTERVAL:
                    last_eval = now_mono
                    now_elapsed, now_wall = get_android_clocks()
                    lock_info = db.sync_monotonic_clock(now_elapsed, now_wall)
                    evaluations, blocked_targets, _ = tracker.evaluate_all_rules(db)
                    db.export_blocker_state(state_json_path, list(blocked_targets.keys()))

                # قراءة التطبيق المفتوح (أولوية لملف Accessibility الفوري، ثم UsageStatsManager)
                fg_pkg = _read_accessibility_fg_pkg(files_dir)
                if not fg_pkg:
                    fg_pkg = tracker.get_foreground_package()

                if fg_pkg and fg_pkg != my_pkg and fg_pkg in blocked_targets:
                    overlay_mgr.show_or_update(fg_pkg, blocked_targets[fg_pkg])
                else:
                    overlay_mgr.hide()

                # تحديث إشعار الخدمة الخلفية كل 10 ثوانٍ
                if now_mono - last_notif_update >= 10.0:
                    last_notif_update = now_mono
                    state = lock_info["state"]
                    if state == LOCK_STATE_WAITING_24H:
                        txt = f"فك القفل بعد: {format_hhmmss(lock_info['remaining_seconds'])}"
                    elif state == LOCK_STATE_UNLOCKED_WINDOW:
                        txt = f"نافذة التعديل مفتوحة: {format_hhmmss(lock_info['remaining_seconds'])}"
                    else:
                        txt = f"الحماية الصارمة مفعلة | تطبيقات محظورة: {len(blocked_targets)}"
                    try:
                        updated_notif, _ = _create_notification(service, "الحظر المشروط", txt)
                        nm.notify(NOTIFICATION_ID, updated_notif)
                    except Exception:
                        traceback.print_exc()

            except Exception:
                traceback.print_exc()

            time.sleep(0.8)
    finally:
        try:
            overlay_mgr.hide()
        except Exception:
            pass
        try:
            if wake_lock is not None and wake_lock.isHeld():
                wake_lock.release()
        except Exception:
            traceback.print_exc()


if __name__ == "__main__":
    run_monitor_service()
