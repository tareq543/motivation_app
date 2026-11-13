import os
import time

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ

if IS_ANDROID:
    from jnius import autoclass

    Context = autoclass("android.content.Context")
    Intent = autoclass("android.content.Intent")
    PackageManager = autoclass("android.content.pm.PackageManager")
    UsageEventsEvent = autoclass("android.app.usage.UsageEvents$Event")
    SystemClock = autoclass("android.os.SystemClock")
    System = autoclass("java.lang.System")
    Bitmap = autoclass("android.graphics.Bitmap")
    BitmapConfig = autoclass("android.graphics.Bitmap$Config")
    CompressFormat = autoclass("android.graphics.Bitmap$CompressFormat")
    Canvas = autoclass("android.graphics.Canvas")
    FileOutputStream = autoclass("java.io.FileOutputStream")

    EVENT_FOREGROUND = 1   # MOVE_TO_FOREGROUND / ACTIVITY_RESUMED
    EVENT_BACKGROUND = 2   # MOVE_TO_BACKGROUND / ACTIVITY_PAUSED
    EVENT_STOPPED = 23     # ACTIVITY_STOPPED (Android 10+)
else:
    EVENT_FOREGROUND = 1
    EVENT_BACKGROUND = 2
    EVENT_STOPPED = 23


def get_android_clocks() -> tuple[int, int]:
    """يرجع (elapsedRealtime_ms, currentTimeMillis_ms)."""
    if IS_ANDROID:
        return int(SystemClock.elapsedRealtime()), int(System.currentTimeMillis())
    now_ms = int(time.time() * 1000)
    return int(time.monotonic() * 1000), now_ms


class UsageTracker:
    def __init__(self, context=None, cache_dir: str = "."):
        self.context = context
        self.cache_dir = cache_dir
        self._app_cache: dict[str, dict] = {}
        self.icons_dir = os.path.join(cache_dir, "app_icons")
        os.makedirs(self.icons_dir, exist_ok=True)

    def _get_context(self):
        if self.context is not None:
            return self.context
        if IS_ANDROID:
            try:
                PythonActivity = autoclass("org.kivy.android.PythonActivity")
                if PythonActivity.mActivity is not None:
                    return PythonActivity.mActivity
            except Exception:
                pass
            try:
                PythonService = autoclass("org.kivy.android.PythonService")
                if PythonService.mService is not None:
                    return PythonService.mService
            except Exception:
                pass
        return None

    def get_installed_apps(self, force_refresh: bool = False) -> list[dict]:
        """
        يجلب قائمة التطبيقات المثبتة القابلة للتشغيل مع الاسم والحزمة ومسار الأيقونة.
        """
        if self._app_cache and not force_refresh:
            return sorted(self._app_cache.values(), key=lambda x: x["name"].lower())

        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return []

        pm = ctx.getPackageManager()
        my_pkg = str(ctx.getPackageName())
        launch_intent = Intent(Intent.ACTION_MAIN, None)
        launch_intent.addCategory(Intent.CATEGORY_LAUNCHER)

        resolve_infos = pm.queryIntentActivities(launch_intent, 0)
        apps_map = {}

        size = resolve_infos.size()
        for i in range(size):
            ri = resolve_infos.get(i)
            act_info = ri.activityInfo
            pkg = str(act_info.packageName)
            if pkg == my_pkg or pkg in apps_map:
                continue

            try:
                label = str(ri.loadLabel(pm))
            except Exception:
                label = pkg

            icon_path = os.path.join(self.icons_dir, f"{pkg}.png")
            if not os.path.exists(icon_path):
                try:
                    drawable = ri.loadIcon(pm)
                    self._save_drawable_to_png(drawable, icon_path)
                except Exception:
                    icon_path = ""

            apps_map[pkg] = {
                "name": label,
                "package_name": pkg,
                "icon_path": icon_path if os.path.exists(icon_path) else "",
            }

        self._app_cache = apps_map
        return sorted(apps_map.values(), key=lambda x: x["name"].lower())

    def get_app_label(self, pkg: str) -> str:
        if pkg in self._app_cache:
            return self._app_cache[pkg]["name"]
        ctx = self._get_context()
        if IS_ANDROID and ctx is not None:
            try:
                pm = ctx.getPackageManager()
                app_info = pm.getApplicationInfo(pkg, 0)
                label = str(pm.getApplicationLabel(app_info))
                self._app_cache[pkg] = {
                    "name": label,
                    "package_name": pkg,
                    "icon_path": os.path.join(self.icons_dir, f"{pkg}.png"),
                }
                return label
            except Exception:
                pass
        return pkg

    def _save_drawable_to_png(self, drawable, out_path: str):
        width = drawable.getIntrinsicWidth()
        height = drawable.getIntrinsicHeight()
        if width <= 0 or height <= 0:
            width, height = 96, 96
        size_px = min(max(width, 64), 128)
        bmp = Bitmap.createBitmap(size_px, size_px, BitmapConfig.ARGB_8888)
        canvas = Canvas(bmp)
        drawable.setBounds(0, 0, size_px, size_px)
        drawable.draw(canvas)
        fos = FileOutputStream(out_path)
        bmp.compress(CompressFormat.PNG, 90, fos)
        fos.flush()
        fos.close()
        bmp.recycle()

    def get_foreground_package(self) -> str:
        """
        يرجع اسم الحزمة المفتوحة حالياً في الواجهة عبر آخر أحداث UsageStatsManager.
        """
        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return ""
        try:
            usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE)
            now_ms = int(System.currentTimeMillis())
            events = usm.queryEvents(now_ms - 15000, now_ms)
            ev = UsageEventsEvent()
            last_pkg = ""
            last_ts = -1
            while events.hasNextEvent():
                events.getNextEvent(ev)
                etype = int(ev.getEventType())
                if etype == EVENT_FOREGROUND:
                    ts = int(ev.getTimeStamp())
                    if ts >= last_ts:
                        last_ts = ts
                        last_pkg = str(ev.getPackageName())
            return last_pkg
        except Exception:
            return ""

    def get_usage_seconds_since_reset(self, db, packages_of_interest: set[str] | None = None) -> dict[str, float]:
        """
        يحسب ثواني الاستخدام الدقيقة لكل تطبيق منذ آخر وقت تصفير يومي مخصص
        باستخدام UsageStatsManager.queryEvents() وحساب الفارق بين FOREGROUND و BACKGROUND.
        """
        now_elapsed, now_wall = get_android_clocks()
        lock_state = db.sync_monotonic_clock(now_elapsed, now_wall)
        trusted_wall = lock_state["trusted_wall_ms"]
        wall_offset = lock_state["wall_offset_ms"]

        cycle_start_trusted, cycle_id = db.get_current_reset_window(trusted_wall)
        cached_usage = db.get_cached_usage(cycle_id)

        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return cached_usage

        # تحويل بداية الدورة إلى نطاق ساعة النظام مع هامش قبلي 12 ساعة لالتقاط الجلسات الممتدة عبر وقت التصفير
        cycle_start_sys = cycle_start_trusted + wall_offset
        query_start = max(0, cycle_start_sys - (12 * 3600 * 1000))
        query_end = now_wall

        if query_end <= cycle_start_sys:
            return cached_usage

        usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE)
        events = usm.queryEvents(query_start, query_end)
        ev = UsageEventsEvent()

        active_start: dict[str, int] = {}
        accumulated_ms: dict[str, int] = {}

        while events.hasNextEvent():
            events.getNextEvent(ev)
            pkg = str(ev.getPackageName())
            if packages_of_interest is not None and pkg not in packages_of_interest:
                continue

            etype = int(ev.getEventType())
            ts = int(ev.getTimeStamp())

            if etype == EVENT_FOREGROUND:
                # تجنب الكتابة فوق جلسة مفتوحة لنفس الحزمة
                if pkg not in active_start:
                    active_start[pkg] = ts
            elif etype in (EVENT_BACKGROUND, EVENT_STOPPED):
                if pkg in active_start:
                    start_ts = active_start.pop(pkg)
                    clamped_start = max(start_ts, cycle_start_sys)
                    clamped_end = min(ts, query_end)
                    if clamped_end > clamped_start:
                        accumulated_ms[pkg] = accumulated_ms.get(pkg, 0) + (clamped_end - clamped_start)
                elif ts > cycle_start_sys:
                    # التطبيق كان مفتوحاً قبل بداية نافذة الاستعلام وأغلق بعد وقت التصفير
                    clamped_end = min(ts, query_end)
                    if clamped_end > cycle_start_sys:
                        accumulated_ms[pkg] = accumulated_ms.get(pkg, 0) + (clamped_end - cycle_start_sys)

        # إذا كان هناك تطبيق لا يزال مفتوحاً في الواجهة حتى هذه اللحظة
        for pkg, start_ts in active_start.items():
            clamped_start = max(start_ts, cycle_start_sys)
            if query_end > clamped_start:
                accumulated_ms[pkg] = accumulated_ms.get(pkg, 0) + (query_end - clamped_start)

        measured_sec = {pkg: ms / 1000.0 for pkg, ms in accumulated_ms.items()}

        # دمج مع الذاكرة المخبأة بحيث لا يتناقص الوقت أبداً إذا حاول المستخدم التلاعب بالساعة
        merged: dict[str, float] = dict(cached_usage)
        for pkg, sec in measured_sec.items():
            merged[pkg] = max(merged.get(pkg, 0.0), sec)

        if merged:
            db.update_cached_usage(cycle_id, merged)

        return merged

    def evaluate_all_rules(self, db) -> tuple[list[dict], dict[str, dict], dict[str, float]]:
        """
        يُقيّم جميع القواعد النشطة ويرجع:
        1) قائمة نتائج القواعد للعرض في الشاشة الرئيسية.
        2) قاموس التطبيقات المحظورة حالياً {target_pkg: blocking_rule_eval}.
        3) قاموس ثواني الاستخدام لجميع التطبيقات ذات الصلة.
        """
        rules = db.get_all_rules()
        needed_pkgs: set[str] = set()
        for r in rules:
            if r["enabled"]:
                needed_pkgs.update(r["source_packages"])

        usage_sec = self.get_usage_seconds_since_reset(db, needed_pkgs if needed_pkgs else None)

        evaluations = []
        blocked_targets: dict[str, dict] = {}

        for r in rules:
            req_min = max(1, int(r["required_minutes"]))
            req_sec = req_min * 60.0
            src_pkgs = r["source_packages"]
            tgt_pkgs = r["target_packages"]
            mode = r["mode"]

            src_breakdown = []
            for sp in src_pkgs:
                used_s = usage_sec.get(sp, 0.0)
                src_breakdown.append({
                    "package_name": sp,
                    "label": self.get_app_label(sp),
                    "used_seconds": used_s,
                    "used_minutes": used_s / 60.0,
                    "required_minutes": req_min,
                    "progress": min(1.0, used_s / req_sec) if req_sec > 0 else 1.0,
                })

            if not r["enabled"]:
                is_unlocked = True
                overall_progress = 1.0
                completed_minutes = float(req_min)
            elif mode == "SUM":
                total_used_s = sum(item["used_seconds"] for item in src_breakdown)
                completed_minutes = total_used_s / 60.0
                overall_progress = min(1.0, total_used_s / req_sec)
                is_unlocked = total_used_s >= req_sec
            else:  # INDIVIDUAL / AND
                if src_breakdown:
                    overall_progress = sum(item["progress"] for item in src_breakdown) / len(src_breakdown)
                    completed_minutes = min(item["used_minutes"] for item in src_breakdown)
                    is_unlocked = all(item["used_seconds"] >= req_sec for item in src_breakdown)
                else:
                    overall_progress = 1.0
                    completed_minutes = float(req_min)
                    is_unlocked = True

            eval_item = {
                "rule": r,
                "is_unlocked": is_unlocked,
                "overall_progress": overall_progress,
                "completed_minutes": completed_minutes,
                "required_minutes": req_min,
                "remaining_minutes": max(0.0, req_min - completed_minutes),
                "source_breakdown": src_breakdown,
            }
            evaluations.append(eval_item)

            if r["enabled"] and not is_unlocked:
                for tp in tgt_pkgs:
                    # في حال وجود أكثر من قاعدة تحظر نفس التطبيق، نحتفظ بأول قاعدة غير مكتملة
                    if tp not in blocked_targets:
                        blocked_targets[tp] = eval_item

        return evaluations, blocked_targets, usage_sec
