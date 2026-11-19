import json
import os
import threading
import time

IS_ANDROID = "ANDROID_ARGUMENT" in os.environ

if IS_ANDROID:
    try:
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
        JNIUS_OK = True
    except Exception:
        # If any class is missing we degrade to desktop behaviour instead of
        # dying here: this module is imported at startup, before any Kivy code
        # or error handler exists, so an exception would kill the app silently.
        import traceback

        traceback.print_exc()
        JNIUS_OK = False
        IS_ANDROID = False
else:
    JNIUS_OK = False

EVENT_FOREGROUND = 1   # MOVE_TO_FOREGROUND / ACTIVITY_RESUMED
EVENT_BACKGROUND = 2   # MOVE_TO_BACKGROUND / ACTIVITY_PAUSED
EVENT_STOPPED = 23     # ACTIVITY_STOPPED (Android 10+)


def get_android_clocks() -> tuple[int, int]:
    """يرجع (elapsedRealtime_ms, currentTimeMillis_ms)."""
    if IS_ANDROID:
        return int(SystemClock.elapsedRealtime()), int(System.currentTimeMillis())
    now_ms = int(time.time() * 1000)
    return int(time.monotonic() * 1000), now_ms


def detach_current_thread():
    """يحرر خيط بايثون من JVM بعد انتهاء العمل في الخلفية.

    pyjnius يربط الخيط بالـ JVM عند أول استدعاء Java ولا يفك الارتباط تلقائياً،
    فتراكم الخيوط المرتبطة كان يسبب كراشات متقطعة بعد كل تحديث في الواجهة.
    """
    if not IS_ANDROID:
        return
    try:
        from jnius import detach

        detach()
    except Exception:
        pass


class UsageTracker:
    def __init__(self, context=None, cache_dir: str = "."):
        self.context = context
        self.cache_dir = cache_dir
        self._app_cache: dict[str, dict] = {}
        self._icons_dir = os.path.join(cache_dir, "app_icons")
        os.makedirs(self._icons_dir, exist_ok=True)
        self._apps_json = os.path.join(cache_dir, "installed_apps.json")
        self._lock = threading.RLock()
        self._query_lock = threading.Lock()
        self._icons: dict[str, str] = {}
        self._usage_memo = None  # (cycle_id, packages_key, ts, usage_copy)
        self._fg_memo = (0.0, "")

    @property
    def icons_dir(self) -> str:
        return self._icons_dir

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

    def _sorted_apps(self) -> list[dict]:
        return sorted(
            self._app_cache.values(),
            key=lambda x: (x.get("name") or "").casefold(),
        )

    def _load_apps_cache(self) -> dict[str, dict]:
        try:
            with open(self._apps_json, "r", encoding="utf-8") as f:
                raw = json.load(f)
            apps: dict[str, dict] = {}
            for item in raw:
                pkg = str(item.get("package_name") or "")
                if not pkg:
                    continue
                apps[pkg] = {
                    "name": str(item.get("name") or pkg),
                    "package_name": pkg,
                    "icon_path": "",
                }
            return apps
        except Exception:
            return {}

    def _store_apps_cache(self, apps: dict[str, dict]):
        try:
            tmp = self._apps_json + ".tmp"
            payload = [
                {"name": a["name"], "package_name": a["package_name"]}
                for a in apps.values()
            ]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._apps_json)
        except Exception:
            pass

    def get_installed_apps(self, force_refresh: bool = False) -> list[dict]:
        """
        يجلب قائمة التطبيقات المثبتة القابلة للتشغيل.

        - عند أول فتح يقرأ نسخة مخزنة على القرص (فتح فوري بدون تجميد).
        - التحديث الكامل من PackageManager يجري في خيط منفصل فقط.
        - لا نستخرج الأيقونات هنا: تُستخرج عند عرض الصف فقط (Lazy).
        """
        with self._lock:
            if self._app_cache and not force_refresh:
                return self._sorted_apps()
            if not force_refresh:
                cached = self._load_apps_cache()
                if cached:
                    self._app_cache = cached
                    return self._sorted_apps()

        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return []

        # استعلام واحد فقط في نفس الوقت. إذا كان هناك استعلام جارٍ (تسخين
        # بدأ عند فتح التطبيق مثلاً) ننتظره ثم نستخدم نتيجته بدل إرجاع قائمة فارغة.
        if not self._query_lock.acquire(blocking=False):
            with self._query_lock:
                with self._lock:
                    if self._app_cache:
                        return self._sorted_apps()
            if not self._query_lock.acquire(blocking=False):
                with self._lock:
                    return self._sorted_apps()
        try:
            apps = self._query_installed_apps(ctx)
            if apps:
                with self._lock:
                    self._app_cache = apps
                self._store_apps_cache(apps)
        finally:
            self._query_lock.release()

        with self._lock:
            return self._sorted_apps()

    def _query_installed_apps(self, ctx) -> dict[str, dict]:
        pm = ctx.getPackageManager()
        my_pkg = str(ctx.getPackageName())
        launch_intent = Intent(Intent.ACTION_MAIN, None)
        launch_intent.addCategory(Intent.CATEGORY_LAUNCHER)

        resolve_infos = pm.queryIntentActivities(launch_intent, 0)
        apps_map: dict[str, dict] = {}
        size = resolve_infos.size()
        for i in range(size):
            try:
                ri = resolve_infos.get(i)
                act_info = ri.activityInfo
                pkg = str(act_info.packageName)
                if pkg == my_pkg or pkg in apps_map:
                    continue
                try:
                    label = str(ri.loadLabel(pm))
                except Exception:
                    label = pkg
                apps_map[pkg] = {
                    "name": label,
                    "package_name": pkg,
                    "icon_path": "",
                }
            except Exception:
                continue
        return apps_map

    def get_app_label(self, pkg: str) -> str:
        with self._lock:
            cached = self._app_cache.get(pkg)
        if cached and cached.get("name"):
            return cached["name"]
        ctx = self._get_context()
        if IS_ANDROID and ctx is not None:
            try:
                pm = ctx.getPackageManager()
                app_info = pm.getApplicationInfo(pkg, 0)
                label = str(pm.getApplicationLabel(app_info))
                with self._lock:
                    self._app_cache[pkg] = {
                        "name": label,
                        "package_name": pkg,
                        "icon_path": "",
                    }
                return label
            except Exception:
                pass
        return pkg

    def get_app_icon_path(self, pkg: str) -> str:
        """يستخرج أيقونة تطبيق واحد فقط عند الحاجة (Lazy) ويخزنها على القرص."""
        if not pkg:
            return ""
        with self._lock:
            cached = self._icons.get(pkg)
        if cached and os.path.exists(cached):
            return cached

        path = os.path.join(self._icons_dir, f"{pkg}.png")
        if os.path.exists(path):
            with self._lock:
                self._icons[pkg] = path
            return path

        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return ""
        try:
            drawable = ctx.getPackageManager().getApplicationIcon(pkg)
            self._save_drawable_to_png(drawable, path)
            if os.path.exists(path):
                with self._lock:
                    self._icons[pkg] = path
                return path
        except Exception:
            pass
        return ""

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
        now_mono = time.monotonic()
        if now_mono - self._fg_memo[0] < 1.0:
            return self._fg_memo[1]
        ctx = self._get_context()
        if not IS_ANDROID or ctx is None:
            return ""
        result = ""
        try:
            usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE)
            if usm is None:
                return ""
            now_ms = int(System.currentTimeMillis())
            events = usm.queryEvents(now_ms - 15000, now_ms)
            if events is None:
                return ""
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
            result = last_pkg
        except Exception:
            result = ""
        self._fg_memo = (now_mono, result)
        return result

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

        pkgs_key = None
        if packages_of_interest is not None:
            pkgs_key = tuple(sorted(packages_of_interest))
        memo = self._usage_memo
        if (
            memo is not None
            and memo[0] == cycle_id
            and memo[1] == pkgs_key
            and time.monotonic() - memo[2] < 1.5
        ):
            return dict(memo[3])

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

        try:
            usm = ctx.getSystemService(Context.USAGE_STATS_SERVICE)
            if usm is None:
                return cached_usage
            events = usm.queryEvents(query_start, query_end)
            if events is None:
                return cached_usage
        except Exception:
            return cached_usage
        ev = UsageEventsEvent()

        active_start: dict[str, int] = {}
        accumulated_ms: dict[str, int] = {}
        # حزم ظهر لها حدث فتح داخل نافذة الاستعلام. أي حدث إغلاق لاحق لها
        # يكون مجرد تكرار (PAUSED ثم STOPPED) ولا يجوز أن يُحسب كجلسة قديمة.
        seen_foreground: set[str] = set()
        # حزم لم يظهر لها فتح داخل النافذة، وافترضنا أنها كانت مفتوحة قبل
        # بداية الدورة؛ نحسبها مرة واحدة فقط حتى لا تتضاعف آلاف المرات.
        prewindow_counted: set[str] = set()

        while events.hasNextEvent():
            events.getNextEvent(ev)
            pkg = str(ev.getPackageName())
            if packages_of_interest is not None and pkg not in packages_of_interest:
                continue

            etype = int(ev.getEventType())
            ts = int(ev.getTimeStamp())

            if etype == EVENT_FOREGROUND:
                seen_foreground.add(pkg)
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
                elif (
                    pkg not in seen_foreground
                    and pkg not in prewindow_counted
                    and ts > cycle_start_sys
                ):
                    # التطبيق كان مفتوحاً قبل بداية نافذة الاستعلام وأغلق بعد
                    # وقت التصفير. هذا الاحتمال نادر جداً، ويُحسب مرة واحدة.
                    prewindow_counted.add(pkg)
                    clamped_end = min(ts, query_end)
                    accumulated_ms[pkg] = accumulated_ms.get(pkg, 0) + (clamped_end - cycle_start_sys)

        # إذا كان هناك تطبيق لا يزال مفتوحاً في الواجهة حتى هذه اللحظة
        for pkg, start_ts in active_start.items():
            clamped_start = max(start_ts, cycle_start_sys)
            if query_end > clamped_start:
                accumulated_ms[pkg] = accumulated_ms.get(pkg, 0) + (query_end - clamped_start)

        # لا يمكن لأي تطبيق أن يُستخدم أكثر من الزمن المنقضي منذ بداية الدورة.
        # هذا السقف يمنع أي قفزة شاذة في أحداث النظام من إظهار آلاف الدقائق.
        max_possible_sec = max(0.0, (query_end - cycle_start_sys) / 1000.0)
        measured_sec = {
            pkg: min(ms / 1000.0, max_possible_sec)
            for pkg, ms in accumulated_ms.items()
        }

        # دمج مع الذاكرة المخبأة بحيث لا يتناقص الوقت أبداً إذا حاول المستخدم التلاعب بالساعة
        merged: dict[str, float] = {
            pkg: min(sec, max_possible_sec) for pkg, sec in cached_usage.items()
        }
        for pkg, sec in measured_sec.items():
            merged[pkg] = min(max(merged.get(pkg, 0.0), sec), max_possible_sec)

        if merged:
            db.update_cached_usage(cycle_id, merged)

        self._usage_memo = (cycle_id, pkgs_key, time.monotonic(), dict(merged))
        return merged

    def evaluate_all_rules(self, db) -> tuple[list[dict], dict[str, dict], dict[str, float]]:
        """
        يُقيّم جميع القواعد النشطة ويرجع:
        1) قائمة نتائج القواعد للعرض في الشاشة الرئيسية.
        2) قاموس التطبيقات المحظورة حالياً {target_pkg: blocking_rule_eval}.
        3) قاموس ثواني الاستخدام لجميع التطبيقات ذات الصلة.
        """
        rules = db.get_all_rules()
        if not rules:
            return [], {}, {}

        needed_pkgs: set[str] = set()
        for r in rules:
            if r["enabled"]:
                needed_pkgs.update(r["source_packages"])

        # لا توجد قاعدة نشطة => لا حاجة لقراءة أحداث الاستخدام إطلاقاً.
        # (سابقاً كان يُمرَّر None فيقرأ النظام استخدام كل التطبيقات لساعات).
        usage_sec = (
            self.get_usage_seconds_since_reset(db, needed_pkgs) if needed_pkgs else {}
        )

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
