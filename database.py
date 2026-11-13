import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta


LOCK_STATE_LOCKED = "LOCKED"
LOCK_STATE_WAITING_24H = "WAITING_24H"
LOCK_STATE_UNLOCKED_WINDOW = "UNLOCKED_WINDOW"

COOLDOWN_24H_SECONDS = 86400.0   # 24 ساعة
GRACE_WINDOW_30M_SECONDS = 1800.0  # 30 دقيقة
TAMPER_THRESHOLD_MS = 120000     # دقيقتان كحد أقصى للانحراف المسموح


class CondBlockerDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=20.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=15000;")
        return conn

    def _init_schema(self):
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS rules (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        target_packages TEXT NOT NULL,
                        source_packages TEXT NOT NULL,
                        required_minutes INTEGER NOT NULL,
                        mode TEXT NOT NULL CHECK(mode IN ('SUM', 'INDIVIDUAL')),
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS lock_state (
                        id INTEGER PRIMARY KEY CHECK(id = 1),
                        state TEXT NOT NULL,
                        remaining_seconds REAL NOT NULL,
                        last_elapsed_ms INTEGER NOT NULL,
                        last_wall_ms INTEGER NOT NULL,
                        trusted_wall_ms INTEGER NOT NULL,
                        clock_tampered INTEGER NOT NULL DEFAULT 0,
                        wall_offset_ms INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_usage_cache (
                        reset_cycle_id TEXT NOT NULL,
                        package_name TEXT NOT NULL,
                        seconds_used REAL NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (reset_cycle_id, package_name)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tamper_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        details TEXT NOT NULL,
                        timestamp_ms INTEGER NOT NULL
                    );
                """)

                defaults = {
                    "reset_hour": "4",
                    "reset_minute": "0",
                    "onboarding_complete": "0",
                    "is_initialized": "0",
                    "last_foreground_pkg": "",
                }
                for k, v in defaults.items():
                    cur.execute(
                        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
                        (k, v),
                    )

                now_wall_ms = int(time.time() * 1000)
                cur.execute("SELECT id FROM lock_state WHERE id = 1;")
                if cur.fetchone() is None:
                    cur.execute("""
                        INSERT INTO lock_state (
                            id, state, remaining_seconds, last_elapsed_ms,
                            last_wall_ms, trusted_wall_ms, clock_tampered,
                            wall_offset_ms, updated_at
                        ) VALUES (1, ?, 0.0, 0, ?, ?, 0, 0, ?);
                    """, (LOCK_STATE_LOCKED, now_wall_ms, now_wall_ms, now_wall_ms))

                conn.commit()
            finally:
                conn.close()

    # -------------------- Settings --------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?;", (key,)
                ).fetchone()
                return row["value"] if row else default
            finally:
                conn.close()

    def set_setting(self, key: str, value: str):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
                    (key, str(value)),
                )
                conn.commit()
            finally:
                conn.close()

    def get_reset_time(self) -> tuple[int, int]:
        h = int(self.get_setting("reset_hour", "4"))
        m = int(self.get_setting("reset_minute", "0"))
        return max(0, min(23, h)), max(0, min(59, m))

    def set_reset_time(self, hour: int, minute: int):
        self.set_setting("reset_hour", str(max(0, min(23, int(hour)))))
        self.set_setting("reset_minute", str(max(0, min(59, int(minute)))))

    def is_onboarding_complete(self) -> bool:
        return self.get_setting("onboarding_complete", "0") == "1"

    def set_onboarding_complete(self, val: bool):
        self.set_setting("onboarding_complete", "1" if val else "0")

    def is_initialized(self) -> bool:
        return self.get_setting("is_initialized", "0") == "1"

    def set_initialized(self, val: bool):
        self.set_setting("is_initialized", "1" if val else "0")

    # -------------------- Tamper Logs --------------------
    def log_tamper(self, event_type: str, details: str, ts_ms: int | None = None):
        with self._lock:
            conn = self._connect()
            try:
                ts = ts_ms if ts_ms is not None else int(time.time() * 1000)
                conn.execute(
                    "INSERT INTO tamper_logs (event_type, details, timestamp_ms) VALUES (?, ?, ?);",
                    (event_type, details, ts),
                )
                conn.commit()
            finally:
                conn.close()

    def get_tamper_logs(self, limit: int = 30) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, event_type, details, timestamp_ms FROM tamper_logs "
                    "ORDER BY id DESC LIMIT ?;",
                    (limit,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

    # -------------------- Anti-Tamper Clock & Strict Lock --------------------
    def sync_monotonic_clock(self, now_elapsed_ms: int, now_wall_ms: int) -> dict:
        """
        يحدث الوقت الموثوق ويحسب التنازلي لدورة 24 ساعة / 30 دقيقة
        بالاعتماد الصارم على SystemClock.elapsedRealtime() لمنع خداع تغيير ساعة الهاتف.
        """
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM lock_state WHERE id = 1;").fetchone()
                state = row["state"]
                remaining = float(row["remaining_seconds"])
                last_elapsed = int(row["last_elapsed_ms"])
                last_wall = int(row["last_wall_ms"])
                trusted_wall = int(row["trusted_wall_ms"])
                clock_tampered = int(row["clock_tampered"])
                wall_offset = int(row["wall_offset_ms"])

                delta_sec = 0.0
                if last_elapsed == 0:
                    # أول تهيئة بعد إنشاء القاعدة
                    trusted_wall = now_wall_ms
                    wall_offset = 0
                    clock_tampered = 0
                elif now_elapsed >= last_elapsed:
                    # نفس جلسة التشغيل (لم يتم إعادة تشغيل الهاتف)
                    real_delta_ms = now_elapsed - last_elapsed
                    delta_sec = real_delta_ms / 1000.0
                    trusted_wall = trusted_wall + real_delta_ms

                    diff_ms = now_wall_ms - trusted_wall
                    if abs(diff_ms) > TAMPER_THRESHOLD_MS:
                        if clock_tampered == 0 or abs(diff_ms - wall_offset) > TAMPER_THRESHOLD_MS:
                            conn.execute(
                                "INSERT INTO tamper_logs (event_type, details, timestamp_ms) VALUES (?, ?, ?);",
                                (
                                    "CLOCK_MANIPULATION",
                                    f"تم اكتشاف تعديل يدوي لساعة الهاتف بمقدار {int(diff_ms / 1000)} ثانية. تم تجاهل التغيير.",
                                    trusted_wall,
                                ),
                            )
                        clock_tampered = 1
                        wall_offset = diff_ms
                    else:
                        clock_tampered = 0
                        wall_offset = 0
                else:
                    # حدثت إعادة تشغيل للهاتف (elapsedRealtime عاد إلى 0)
                    boot_elapsed_ms = now_elapsed
                    wall_gap_ms = now_wall_ms - last_wall
                    if (
                        clock_tampered == 0
                        and 0 < wall_gap_ms < (7 * 86400 * 1000)
                        and wall_gap_ms >= boot_elapsed_ms
                    ):
                        # التقدم الفعلي خلال الإطفاء + التشغيل
                        trusted_wall = trusted_wall + wall_gap_ms
                        delta_sec = wall_gap_ms / 1000.0
                    else:
                        # في حال وجود تلاعب مسبق أو ارتداد ساعة، نحتسب وقت التشغيل فقط
                        trusted_wall = trusted_wall + boot_elapsed_ms
                        delta_sec = boot_elapsed_ms / 1000.0

                # تحديث مؤقتات القفل الصارم
                if delta_sec > 0:
                    if state == LOCK_STATE_WAITING_24H:
                        remaining -= delta_sec
                        if remaining <= 0:
                            # انتهت الـ 24 ساعة -> فتح نافذة الـ 30 دقيقة
                            overflow = abs(remaining)
                            state = LOCK_STATE_UNLOCKED_WINDOW
                            remaining = max(0.0, GRACE_WINDOW_30M_SECONDS - overflow)
                            if remaining <= 0:
                                state = LOCK_STATE_LOCKED
                                remaining = 0.0
                    elif state == LOCK_STATE_UNLOCKED_WINDOW:
                        remaining -= delta_sec
                        if remaining <= 0:
                            # انتهت نافذة الـ 30 دقيقة -> العودة الفورية للقفل الصارم
                            state = LOCK_STATE_LOCKED
                            remaining = 0.0

                conn.execute("""
                    UPDATE lock_state
                    SET state = ?, remaining_seconds = ?, last_elapsed_ms = ?,
                        last_wall_ms = ?, trusted_wall_ms = ?, clock_tampered = ?,
                        wall_offset_ms = ?, updated_at = ?
                    WHERE id = 1;
                """, (
                    state,
                    remaining,
                    now_elapsed_ms,
                    now_wall_ms,
                    trusted_wall,
                    clock_tampered,
                    wall_offset,
                    trusted_wall,
                ))
                conn.commit()

                return {
                    "state": state,
                    "remaining_seconds": remaining,
                    "trusted_wall_ms": trusted_wall,
                    "clock_tampered": bool(clock_tampered),
                    "wall_offset_ms": wall_offset,
                }
            finally:
                conn.close()

    def get_lock_state(self) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM lock_state WHERE id = 1;").fetchone()
                return dict(row)
            finally:
                conn.close()

    def request_unlock_24h(self, now_elapsed_ms: int, now_wall_ms: int) -> dict:
        self.sync_monotonic_clock(now_elapsed_ms, now_wall_ms)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT * FROM lock_state WHERE id = 1;").fetchone()
                if row["state"] == LOCK_STATE_LOCKED:
                    conn.execute("""
                        UPDATE lock_state
                        SET state = ?, remaining_seconds = ?, last_elapsed_ms = ?, last_wall_ms = ?
                        WHERE id = 1;
                    """, (LOCK_STATE_WAITING_24H, COOLDOWN_24H_SECONDS, now_elapsed_ms, now_wall_ms))
                    conn.commit()
                return dict(conn.execute("SELECT * FROM lock_state WHERE id = 1;").fetchone())
            finally:
                conn.close()

    def cancel_unlock_request(self) -> dict:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    UPDATE lock_state
                    SET state = ?, remaining_seconds = 0.0
                    WHERE id = 1;
                """, (LOCK_STATE_LOCKED,))
                conn.commit()
                return dict(conn.execute("SELECT * FROM lock_state WHERE id = 1;").fetchone())
            finally:
                conn.close()

    def can_modify_restrictions(self) -> bool:
        """
        يسمح بالتعديل فقط في نافذة الـ 30 دقيقة، أو عند الإعداد الأولي قبل إضافة أي قاعدة.
        """
        lock_info = self.get_lock_state()
        if lock_info["state"] == LOCK_STATE_UNLOCKED_WINDOW:
            return True
        if not self.is_initialized() and len(self.get_all_rules()) == 0:
            return True
        return False

    def is_protected_locked(self) -> bool:
        lock_info = self.get_lock_state()
        return lock_info["state"] != LOCK_STATE_UNLOCKED_WINDOW

    # -------------------- Reset Cycle & Daily Cache --------------------
    def get_current_reset_window(self, trusted_wall_ms: int) -> tuple[int, str]:
        """
        يحسب الطابع الزمني لآخر وقت تصفير يومي مخصص بالاعتماد على الوقت الموثوق.
        """
        reset_h, reset_m = self.get_reset_time()
        dt_now = datetime.fromtimestamp(trusted_wall_ms / 1000.0)
        reset_today = dt_now.replace(hour=reset_h, minute=reset_m, second=0, microsecond=0)
        if dt_now < reset_today:
            reset_start = reset_today - timedelta(days=1)
        else:
            reset_start = reset_today
        cycle_start_ms = int(reset_start.timestamp() * 1000)
        cycle_id = reset_start.strftime("%Y-%m-%d_%H:%M")
        return cycle_start_ms, cycle_id

    def get_cached_usage(self, cycle_id: str) -> dict[str, float]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT package_name, seconds_used FROM daily_usage_cache WHERE reset_cycle_id = ?;",
                    (cycle_id,),
                ).fetchall()
                return {r["package_name"]: float(r["seconds_used"]) for r in rows}
            finally:
                conn.close()

    def update_cached_usage(self, cycle_id: str, usage_map: dict[str, float]):
        with self._lock:
            conn = self._connect()
            try:
                for pkg, sec in usage_map.items():
                    conn.execute("""
                        INSERT INTO daily_usage_cache (reset_cycle_id, package_name, seconds_used)
                        VALUES (?, ?, ?)
                        ON CONFLICT(reset_cycle_id, package_name)
                        DO UPDATE SET seconds_used = MAX(daily_usage_cache.seconds_used, excluded.seconds_used);
                    """, (cycle_id, pkg, float(sec)))
                # تنظيف الدورات القديمة
                conn.execute(
                    "DELETE FROM daily_usage_cache WHERE reset_cycle_id != ?;",
                    (cycle_id,),
                )
                conn.commit()
            finally:
                conn.close()

    # -------------------- Rules CRUD --------------------
    def get_all_rules(self) -> list[dict]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT * FROM rules ORDER BY id ASC;").fetchall()
                result = []
                for r in rows:
                    d = dict(r)
                    d["target_packages"] = json.loads(d["target_packages"])
                    d["source_packages"] = json.loads(d["source_packages"])
                    d["enabled"] = bool(d["enabled"])
                    result.append(d)
                return result
            finally:
                conn.close()

    def get_rule(self, rule_id: int) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                r = conn.execute("SELECT * FROM rules WHERE id = ?;", (rule_id,)).fetchone()
                if not r:
                    return None
                d = dict(r)
                d["target_packages"] = json.loads(d["target_packages"])
                d["source_packages"] = json.loads(d["source_packages"])
                d["enabled"] = bool(d["enabled"])
                return d
            finally:
                conn.close()

    def create_rule(
        self,
        name: str,
        target_packages: list[str],
        source_packages: list[str],
        required_minutes: int,
        mode: str,
        enabled: bool = True,
    ) -> int:
        with self._lock:
            conn = self._connect()
            try:
                now_ms = int(time.time() * 1000)
                cur = conn.execute("""
                    INSERT INTO rules (
                        name, target_packages, source_packages,
                        required_minutes, mode, enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (
                    name.strip(),
                    json.dumps(list(dict.fromkeys(target_packages))),
                    json.dumps(list(dict.fromkeys(source_packages))),
                    max(1, int(required_minutes)),
                    mode if mode in ("SUM", "INDIVIDUAL") else "SUM",
                    1 if enabled else 0,
                    now_ms,
                    now_ms,
                ))
                conn.commit()
                self.set_initialized(True)
                return cur.lastrowid
            finally:
                conn.close()

    def update_rule(
        self,
        rule_id: int,
        name: str,
        target_packages: list[str],
        source_packages: list[str],
        required_minutes: int,
        mode: str,
        enabled: bool = True,
    ):
        with self._lock:
            conn = self._connect()
            try:
                now_ms = int(time.time() * 1000)
                conn.execute("""
                    UPDATE rules
                    SET name = ?, target_packages = ?, source_packages = ?,
                        required_minutes = ?, mode = ?, enabled = ?, updated_at = ?
                    WHERE id = ?;
                """, (
                    name.strip(),
                    json.dumps(list(dict.fromkeys(target_packages))),
                    json.dumps(list(dict.fromkeys(source_packages))),
                    max(1, int(required_minutes)),
                    mode if mode in ("SUM", "INDIVIDUAL") else "SUM",
                    1 if enabled else 0,
                    now_ms,
                    rule_id,
                ))
                conn.commit()
            finally:
                conn.close()

    def toggle_rule(self, rule_id: int, enabled: bool):
        with self._lock:
            conn = self._connect()
            try:
                now_ms = int(time.time() * 1000)
                conn.execute(
                    "UPDATE rules SET enabled = ?, updated_at = ? WHERE id = ?;",
                    (1 if enabled else 0, now_ms, rule_id),
                )
                conn.commit()
            finally:
                conn.close()

    def delete_rule(self, rule_id: int):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM rules WHERE id = ?;", (rule_id,))
                conn.commit()
            finally:
                conn.close()

    # -------------------- Cross-Process State Sync --------------------
    def export_blocker_state(self, state_file_path: str, blocked_packages: list[str]):
        """
        كتابة ملف حالة مشترك (Atomic Write) لتقرأه خدمات Java (AccessibilityService & DeviceAdmin) فوراً.
        """
        lock_info = self.get_lock_state()
        payload = {
            "lock_state": lock_info["state"],
            "remaining_seconds": lock_info["remaining_seconds"],
            "onboarding_complete": self.is_onboarding_complete(),
            "blocked_packages": blocked_packages,
            "updated_at": int(time.time() * 1000),
        }
        tmp_path = state_file_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(state_file_path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp_path, state_file_path)
        except Exception:
            pass
