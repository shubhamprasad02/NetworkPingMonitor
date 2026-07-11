"""SQLite storage layer for NetworkPingMonitor.

Single database file: data/pingmonitor.db
Designed for straightforward migration to PostgreSQL later.
"""

import base64
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# On Render, set DATA_DIR to the mount path of a Persistent Disk (e.g. /var/data)
# in the service's Environment tab so the database survives restarts/redeploys.
# Without a persistent disk, Render's filesystem is wiped on every deploy/restart.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "pingmonitor.db"
LEGACY_DB_FILE = DATA_DIR / "netwatch.db"
STORE_FILE = DATA_DIR / "store.json"
STORE_BACKUP_FILE = DATA_DIR / "store.json.bak"
USERS_FILE = DATA_DIR / "users.json"
SESSIONS_FILE = DATA_DIR / "sessions.json"
TENANTS_DIR = DATA_DIR / "tenants"

DB_LOCK = threading.Lock()
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_TTL_HOURS = 72

DEFAULT_SETTINGS = {
    "timezone": "Asia/Kolkata",
    "refresh_interval": 5,
}

DEFAULT_ALERTS = {
    "primary_email": "",
    "additional_emails": "",
    "long_outage_alert_seconds": 30,
    "send_short_outage_reports": True,
}


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def make_id(prefix):
    return f"{prefix}_{secrets.token_hex(8)}"


def get_connection():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DB_LOCK:
        conn = get_connection()
        cursor = conn.cursor()

        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                username TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                company_name TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS companies (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                email TEXT,
                receivers TEXT,
                alert_after_seconds INTEGER NOT NULL DEFAULT 30,
                online_alerts INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '-',
                name TEXT NOT NULL,
                ip TEXT NOT NULL,
                muted INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(company_id, ip)
            );

            CREATE TABLE IF NOT EXISTS ping_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                latency TEXT,
                downtime TEXT,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                company_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                date TEXT,
                location TEXT,
                name TEXT,
                ip TEXT,
                offline TEXT,
                online TEXT,
                downtime TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                company_id TEXT,
                primary_email TEXT NOT NULL DEFAULT '',
                additional_emails TEXT NOT NULL DEFAULT '',
                long_outage_alert_seconds INTEGER NOT NULL DEFAULT 30,
                send_short_outage_reports INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
                UNIQUE(user_id, company_id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                user_id TEXT PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                refresh_interval INTEGER NOT NULL DEFAULT 5,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                id TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_companies_user_id ON companies(user_id);
            CREATE INDEX IF NOT EXISTS idx_devices_company_id ON devices(company_id);
            CREATE INDEX IF NOT EXISTS idx_devices_user_id ON devices(user_id);
            CREATE TABLE IF NOT EXISTS daily_summary (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                date TEXT NOT NULL,
                total_checks INTEGER NOT NULL DEFAULT 0,
                successful_checks INTEGER NOT NULL DEFAULT 0,
                failed_checks INTEGER NOT NULL DEFAULT 0,
                avg_latency_ms REAL,
                total_outage_seconds REAL NOT NULL DEFAULT 0,
                sessions_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(device_id, date)
            );

            CREATE INDEX IF NOT EXISTS idx_ping_logs_device_ts ON ping_logs(device_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_ping_logs_user_id ON ping_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_reports_company_id ON reports(company_id);
            CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_daily_summary_device_date ON daily_summary(device_id, date);

            -- Permanent outage history. Deliberately has NO foreign keys and is
            -- NEVER deleted by device/company/user cleanup, so the record of an
            -- outage survives even if the device is later removed. One row per
            -- completed outage (written the moment a device recovers).
            CREATE TABLE IF NOT EXISTS outage_history (
                outage_id TEXT PRIMARY KEY,
                user_id TEXT,
                company_id TEXT,
                device_id TEXT,
                device_name TEXT,
                ip_address TEXT,
                location TEXT,
                offline_start TEXT NOT NULL,
                online_time TEXT NOT NULL,
                outage_duration_seconds INTEGER NOT NULL,
                outage_duration_readable TEXT,
                date TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_outage_history_device_id ON outage_history(device_id);
            CREATE INDEX IF NOT EXISTS idx_outage_history_user_id ON outage_history(user_id);
            CREATE INDEX IF NOT EXISTS idx_outage_history_company_id ON outage_history(company_id);
            CREATE INDEX IF NOT EXISTS idx_outage_history_date ON outage_history(date);

            -- Diagnostics-only log of Minor Interruptions (confirmed drops that
            -- recovered before the Verified Outage threshold). These are
            -- intentionally kept OUT of reports/outage_history and never
            -- affect availability -- they exist solely so the monitoring loop
            -- can detect Network Fluctuation (many short drops in a rolling
            -- window) and so a technician can see diagnostic detail if needed.
            CREATE TABLE IF NOT EXISTS minor_interruptions (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                user_id TEXT,
                company_id TEXT,
                device_name TEXT,
                ip_address TEXT,
                location TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                cause TEXT NOT NULL DEFAULT 'short',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_minor_interruptions_device_time ON minor_interruptions(device_id, start_time);

            -- Forgot-password: short-lived one-time codes emailed to the
            -- account's registered address. Never stores the code itself,
            -- only a salted hash of it (same treatment as real passwords).
            CREATE TABLE IF NOT EXISTS password_resets (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_password_resets_user_id ON password_resets(user_id);
            """
        )

        conn.commit()
        conn.close()


def _migration_done(conn, migration_id):
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE id = ?",
        (migration_id,),
    ).fetchone()
    return row is not None


def _mark_migration_done(conn, migration_id):
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, ?)",
        (migration_id, now_iso()),
    )


def run_migrations():
    init_db()
    migrate_legacy_sqlite()
    migrate_json_store()
    migrate_auth_json()
    migrate_tenant_files()
    migrate_add_report_date_iso()
    migrate_add_username()
    migrate_add_online_alerts()
    migrate_add_daily_summary_extended_stats()
    migrate_add_minor_interruption_cause()


def migrate_add_minor_interruption_cause():
    """Add a `cause` column to minor_interruptions distinguishing a short
    blip ('short') from an interruption that happened while the monitoring
    server's own internet connection was down ('self_internet'), or one a
    user manually moved down from Major ('manual'). Powers the Minor/Major
    incident-log split and the "why is this minor" label shown per row."""
    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "minor_interruptions_cause"):
            conn.close()
            return
        try:
            conn.execute("ALTER TABLE minor_interruptions ADD COLUMN cause TEXT NOT NULL DEFAULT 'short'")
        except sqlite3.OperationalError:
            pass  # column already exists
        _mark_migration_done(conn, "minor_interruptions_cause")
        conn.commit()
        conn.close()


def migrate_add_username():
    """Add a unique username column to users so people can log in with
    either their email or a short user ID, and backfill one for any
    accounts created before this feature existed."""
    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "users_username"):
            conn.close()
            return
        try:
            conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        rows = conn.execute(
            "SELECT id, name, email FROM users WHERE username IS NULL OR username = ''"
        ).fetchall()
        for row in rows:
            username = _generate_unique_username(conn, row["name"], row["email"])
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, row["id"]))

        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)")
        except sqlite3.OperationalError:
            pass

        _mark_migration_done(conn, "users_username")
        conn.commit()
        conn.close()


def migrate_add_online_alerts():
    """Add the online_alerts toggle to companies for in-app/browser alerts,
    separate from the email alert settings."""
    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "companies_online_alerts"):
            conn.close()
            return
        try:
            conn.execute("ALTER TABLE companies ADD COLUMN online_alerts INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # column already exists
        _mark_migration_done(conn, "companies_online_alerts")
        conn.commit()
        conn.close()


def migrate_add_report_date_iso():
    """Add a sortable ISO date column to `reports` so the incident log can be
    filtered by date/date-range. The existing `date` column is a display
    string like '05-07-2026', which sorts and filters poorly."""
    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "reports_date_iso"):
            conn.close()
            return
        try:
            conn.execute("ALTER TABLE reports ADD COLUMN date_iso TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists

        rows = conn.execute(
            "SELECT id, date FROM reports WHERE date_iso IS NULL OR date_iso = ''"
        ).fetchall()
        for row in rows:
            iso = None
            if row["date"]:
                try:
                    iso = datetime.strptime(row["date"], "%d-%m-%Y").strftime("%Y-%m-%d")
                except ValueError:
                    iso = None
            conn.execute("UPDATE reports SET date_iso = ? WHERE id = ?", (iso, row["id"]))

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reports_date_iso ON reports(date_iso)"
        )
        _mark_migration_done(conn, "reports_date_iso")
        conn.commit()
        conn.close()


def migrate_add_daily_summary_extended_stats():
    """Add richer long-term stat columns to daily_summary: min/max latency,
    outage count, longest/shortest single outage, and uptime/downtime
    percentages. Existing rows (days already rolled up before this migration)
    are left NULL for these new columns since their raw ping_logs rows have
    already been pruned and can't be recomputed — everything rolled up from
    this point forward will have them populated."""
    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "daily_summary_extended_stats"):
            conn.close()
            return

        new_columns = [
            ("minimum_latency_ms", "REAL"),
            ("maximum_latency_ms", "REAL"),
            ("outage_count", "INTEGER NOT NULL DEFAULT 0"),
            ("longest_outage_seconds", "REAL"),
            ("shortest_outage_seconds", "REAL"),
            ("outage_periods_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("total_downtime_minutes", "REAL"),
            ("uptime_percentage", "REAL"),
            ("downtime_percentage", "REAL"),
        ]
        for column_name, column_def in new_columns:
            try:
                conn.execute(f"ALTER TABLE daily_summary ADD COLUMN {column_name} {column_def}")
            except sqlite3.OperationalError:
                pass  # column already exists

        _mark_migration_done(conn, "daily_summary_extended_stats")
        conn.commit()
        conn.close()


def migrate_legacy_sqlite():
    if not LEGACY_DB_FILE.exists():
        return

    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "legacy_netwatch_db"):
            conn.close()
            return
        conn.close()

    print("Migrating data from netwatch.db to pingmonitor.db...")
    legacy = sqlite3.connect(LEGACY_DB_FILE)
    legacy.row_factory = sqlite3.Row

    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "legacy_netwatch_db"):
            conn.close()
            legacy.close()
            return

        for table in ("users", "companies", "devices", "reports"):
            try:
                rows = legacy.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                rows = []

            for row in rows:
                data = dict(row)
                if table == "users":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO users (id, name, email, password_hash, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            data["id"],
                            data.get("name") or "User",
                            data["email"],
                            data["password_hash"],
                            data.get("created_at") or now_iso(),
                        ),
                    )
                elif table == "companies":
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO companies
                        (id, user_id, name, email, receivers, alert_after_seconds, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data["id"],
                            data["user_id"],
                            data["name"],
                            data.get("email"),
                            data.get("receivers"),
                            data.get("alert_after_seconds", 30),
                            data.get("created_at") or now_iso(),
                        ),
                    )
                elif table == "devices":
                    user_id = conn.execute(
                        "SELECT user_id FROM companies WHERE id = ?",
                        (data["company_id"],),
                    ).fetchone()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO devices
                        (id, company_id, user_id, location, name, ip, muted, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data["id"],
                            data["company_id"],
                            user_id[0] if user_id else "",
                            data.get("location", "-"),
                            data["name"],
                            data["ip"],
                            data.get("muted", 0),
                            data.get("created_at") or now_iso(),
                        ),
                    )
                elif table == "reports":
                    user_id = conn.execute(
                        "SELECT user_id FROM companies WHERE id = ?",
                        (data["company_id"],),
                    ).fetchone()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO reports
                        (id, company_id, device_id, user_id, date, location, name, ip,
                         offline, online, downtime, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            data["id"],
                            data["company_id"],
                            data["device_id"],
                            user_id[0] if user_id else "",
                            data.get("date"),
                            data.get("location"),
                            data.get("name"),
                            data.get("ip"),
                            data.get("offline"),
                            data.get("online"),
                            data.get("downtime"),
                            data.get("created_at") or now_iso(),
                        ),
                    )

        ping_table = "pings"
        try:
            legacy.execute("SELECT 1 FROM pings LIMIT 1")
        except sqlite3.OperationalError:
            ping_table = None

        if ping_table:
            for row in legacy.execute("SELECT * FROM pings").fetchall():
                data = dict(row)
                device = conn.execute(
                    """
                    SELECT d.id, c.user_id FROM devices d
                    JOIN companies c ON d.company_id = c.id
                    WHERE d.id = ?
                    """,
                    (data["device_id"],),
                ).fetchone()
                if not device:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ping_logs
                    (id, device_id, user_id, timestamp, status, latency, downtime)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        data["id"],
                        data["device_id"],
                        device[1],
                        data["timestamp"],
                        data["status"],
                        data.get("latency"),
                        data.get("downtime"),
                    ),
                )

        _mark_migration_done(conn, "legacy_netwatch_db")
        conn.commit()
        conn.close()

    legacy.close()
    print("Legacy netwatch.db migration complete.")


def _load_json_store(path):
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def migrate_json_store():
    store = _load_json_store(STORE_FILE) or _load_json_store(STORE_BACKUP_FILE)
    if not store:
        return

    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "json_store"):
            conn.close()
            return

        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count > 0 and not store.get("users"):
            _mark_migration_done(conn, "json_store")
            conn.commit()
            conn.close()
            return

        print("Migrating data from store.json to pingmonitor.db...")

        for user in store.get("users", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO users (id, name, email, password_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    user.get("name") or "User",
                    user["email"],
                    user["password_hash"],
                    user.get("created_at") or now_iso(),
                ),
            )

        for company in store.get("companies", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO companies
                (id, user_id, name, email, receivers, alert_after_seconds, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company["id"],
                    company["user_id"],
                    company["name"],
                    company.get("email"),
                    company.get("receivers"),
                    company.get("alert_after_seconds", 30),
                    company.get("created_at") or now_iso(),
                ),
            )

        for device in store.get("devices", []):
            user_id = conn.execute(
                "SELECT user_id FROM companies WHERE id = ?",
                (device["company_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO devices
                (id, company_id, user_id, location, name, ip, muted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device["id"],
                    device["company_id"],
                    user_id[0] if user_id else "",
                    device.get("location", "-"),
                    device["name"],
                    device["ip"],
                    device.get("muted", 0),
                    device.get("created_at") or now_iso(),
                ),
            )

        for report in store.get("reports", []):
            user_id = conn.execute(
                "SELECT user_id FROM companies WHERE id = ?",
                (report["company_id"],),
            ).fetchone()
            conn.execute(
                """
                INSERT OR IGNORE INTO reports
                (id, company_id, device_id, user_id, date, location, name, ip,
                 offline, online, downtime, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["id"],
                    report["company_id"],
                    report["device_id"],
                    user_id[0] if user_id else "",
                    report.get("date"),
                    report.get("location"),
                    report.get("name"),
                    report.get("ip"),
                    report.get("offline"),
                    report.get("online"),
                    report.get("downtime"),
                    report.get("created_at") or now_iso(),
                ),
            )

        _mark_migration_done(conn, "json_store")
        conn.commit()
        conn.close()

    if STORE_FILE.exists():
        try:
            STORE_FILE.rename(STORE_FILE.with_suffix(".json.bak"))
        except OSError:
            pass


def migrate_auth_json():
    if not USERS_FILE.exists() and not SESSIONS_FILE.exists():
        return

    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "auth_json"):
            conn.close()
            return

        if USERS_FILE.exists():
            with USERS_FILE.open("r", encoding="utf-8") as file:
                payload = json.load(file)

            for user in payload.get("users", []):
                password_hash = user.get("password_hash", "")
                salt = user.get("salt")
                if salt and ":" not in password_hash:
                    password_hash = f"{salt}:{password_hash}"

                conn.execute(
                    """
                    INSERT OR IGNORE INTO users
                    (id, name, email, password_hash, company_name, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user["id"],
                        user.get("company") or user.get("name") or "User",
                        user["email"],
                        password_hash,
                        user.get("company"),
                        user.get("created_at") or now_iso(),
                    ),
                )
                ensure_default_company(conn, user["id"], user.get("company") or "My Company")
                ensure_user_settings(conn, user["id"])
                ensure_user_alerts(conn, user["id"])

        if SESSIONS_FILE.exists():
            with SESSIONS_FILE.open("r", encoding="utf-8") as file:
                payload = json.load(file)

            for token, session in payload.items():
                conn.execute(
                    """
                    INSERT OR IGNORE INTO sessions (token, user_id, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        token,
                        session["user_id"],
                        session.get("expires_at") or now_iso(),
                        now_iso(),
                    ),
                )

        _mark_migration_done(conn, "auth_json")
        conn.commit()
        conn.close()


def migrate_tenant_files():
    if not TENANTS_DIR.exists():
        return

    with DB_LOCK:
        conn = get_connection()
        if _migration_done(conn, "tenant_files"):
            conn.close()
            return

        for tenant_path in TENANTS_DIR.iterdir():
            if not tenant_path.is_dir():
                continue

            user_id = tenant_path.name
            user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user:
                continue

            company_id = ensure_default_company(conn, user_id)

            settings_path = tenant_path / "settings.json"
            if settings_path.exists():
                with settings_path.open("r", encoding="utf-8") as file:
                    settings = {**DEFAULT_SETTINGS, **json.load(file)}
                conn.execute(
                    """
                    INSERT OR IGNORE INTO settings (user_id, timezone, refresh_interval)
                    VALUES (?, ?, ?)
                    """,
                    (user_id, settings["timezone"], settings["refresh_interval"]),
                )

            alerts_path = tenant_path / "alerts.json"
            if alerts_path.exists():
                with alerts_path.open("r", encoding="utf-8") as file:
                    alerts = {**DEFAULT_ALERTS, **json.load(file)}
                conn.execute(
                    """
                    INSERT OR IGNORE INTO alerts
                    (id, user_id, company_id, primary_email, additional_emails,
                     long_outage_alert_seconds, send_short_outage_reports)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        make_id("alr"),
                        user_id,
                        company_id,
                        alerts.get("primary_email", ""),
                        alerts.get("additional_emails", ""),
                        alerts.get("long_outage_alert_seconds", 30),
                        1 if alerts.get("send_short_outage_reports", True) else 0,
                    ),
                )

            devices_path = tenant_path / "devices.csv"
            if devices_path.exists():
                with devices_path.open("r", newline="", encoding="utf-8-sig") as file:
                    reader = csv.DictReader(file)
                    for row in reader:
                        ip = (row.get("IP") or row.get("ip") or "").strip()
                        if not ip:
                            continue
                        location = (row.get("Location") or row.get("location") or "-").strip() or "-"
                        name = (row.get("Name") or row.get("name") or "Unnamed Device").strip()
                        existing = conn.execute(
                            "SELECT id FROM devices WHERE company_id = ? AND ip = ?",
                            (company_id, ip),
                        ).fetchone()
                        if existing:
                            continue
                        conn.execute(
                            """
                            INSERT INTO devices
                            (id, company_id, user_id, location, name, ip, muted, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                            """,
                            (make_id("dev"), company_id, user_id, location, name, ip, now_iso()),
                        )

            reports_path = tenant_path / "outage_report.csv"
            if reports_path.exists():
                with reports_path.open("r", newline="", encoding="utf-8-sig") as file:
                    reader = csv.DictReader(file)
                    for row in reader:
                        ip = (row.get("IP Address") or row.get("ip") or "").strip()
                        if not ip:
                            continue
                        device = conn.execute(
                            "SELECT id FROM devices WHERE company_id = ? AND ip = ?",
                            (company_id, ip),
                        ).fetchone()
                        device_id = device[0] if device else make_id("dev")
                        if not device:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO devices
                                (id, company_id, user_id, location, name, ip, muted, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                                """,
                                (
                                    device_id,
                                    company_id,
                                    user_id,
                                    row.get("Location", "-") or "-",
                                    row.get("Device Name", "Unknown") or "Unknown",
                                    ip,
                                    now_iso(),
                                ),
                            )
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO reports
                            (id, company_id, device_id, user_id, date, location, name, ip,
                             offline, online, downtime, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                make_id("rep"),
                                company_id,
                                device_id,
                                user_id,
                                row.get("Date", ""),
                                row.get("Location", ""),
                                row.get("Device Name", ""),
                                ip,
                                row.get("Offline Time", ""),
                                row.get("Online Time", ""),
                                row.get("Downtime", ""),
                                now_iso(),
                            ),
                        )

        _mark_migration_done(conn, "tenant_files")
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"


def verify_password(password, stored_hash):
    if not stored_hash:
        return False

    if ":" in stored_hash:
        try:
            salt_text, digest_text = stored_hash.split(":", 1)
            if re.fullmatch(r"[0-9a-fA-F]+", salt_text) and re.fullmatch(r"[0-9a-fA-F]+", digest_text):
                salt = salt_text.encode("utf-8")
                expected = bytes.fromhex(digest_text)
                actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
                return hmac.compare_digest(actual, expected)

            salt = base64.b64decode(salt_text)
            expected = base64.b64decode(digest_text)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
            return hmac.compare_digest(actual, expected)
        except Exception:
            return False

    return False


# ---------------------------------------------------------------------------
# Forgot password
# ---------------------------------------------------------------------------

RESET_CODE_TTL_MINUTES = 15


def create_password_reset_code(user_id):
    """Generate a 6-digit reset code, store only its hash (with a 15-minute
    expiry), and return the plain code so the caller can email it."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    expires_at = (datetime.now() + timedelta(minutes=RESET_CODE_TTL_MINUTES)).isoformat(timespec="seconds")
    with DB_LOCK:
        conn = get_connection()
        # Invalidate any earlier unused codes for this user so only the
        # most recently requested code can ever succeed.
        conn.execute(
            "UPDATE password_resets SET used = 1 WHERE user_id = ? AND used = 0",
            (user_id,),
        )
        conn.execute(
            """
            INSERT INTO password_resets (id, user_id, code_hash, expires_at, used, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (make_id("pwr"), user_id, hash_password(code), expires_at, now_iso()),
        )
        conn.commit()
        conn.close()
    return code


def verify_and_consume_reset_code(user_id, code):
    """Checks the given code against the newest unused, unexpired reset
    request for this user. On success, marks it used (one-time) and returns
    True. Returns False for a wrong, already-used, or expired code."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT id, code_hash, expires_at FROM password_resets
            WHERE user_id = ? AND used = 0
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if not row:
            conn.close()
            return False

        if datetime.now() > datetime.fromisoformat(row["expires_at"]):
            conn.close()
            return False

        if not verify_password(code, row["code_hash"]):
            conn.close()
            return False

        conn.execute("UPDATE password_resets SET used = 1 WHERE id = ?", (row["id"],))
        conn.commit()
        conn.close()
    return True


def reset_user_password(user_id, new_password):
    """Sets a new password hash and, for safety, signs the user out of every
    existing session (so a leaked/guessed old session can't outlive a reset).
    Named distinctly from the pre-existing `update_user_password` below,
    which expects an already-hashed value rather than a plain password."""
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(new_password), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _slugify_username_base(name, email):
    base = re.sub(r"[^a-z0-9]", "", (name or "").strip().lower())
    if not base:
        base = re.sub(r"[^a-z0-9]", "", (email or "").split("@")[0].lower())
    if not base:
        base = "user"
    return base[:16]


def _generate_unique_username(conn, name, email):
    base = _slugify_username_base(name, email)
    candidate = f"{base}{secrets.randbelow(9000) + 1000}"
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (candidate,)).fetchone():
        candidate = f"{base}{secrets.randbelow(90000) + 10000}"
    return candidate


def is_username_available(username):
    username = (username or "").strip().lower()
    if not username or not re.fullmatch(r"[a-z0-9_.-]{3,24}", username):
        return False
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT 1 FROM users WHERE LOWER(username) = ?", (username,)).fetchone()
        conn.close()
    return row is None


def suggest_username(name, email):
    with DB_LOCK:
        conn = get_connection()
        candidate = _generate_unique_username(conn, name, email)
        conn.close()
    return candidate


def get_user_by_email(email):
    email = email.strip().lower()
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email) = ?",
            (email,),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def get_user_by_username(username):
    username = (username or "").strip().lower()
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(username) = ?",
            (username,),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def get_user_by_identifier(identifier):
    """Look up a user by email OR username, whichever they typed at login."""
    identifier = (identifier or "").strip().lower()
    if not identifier:
        return None
    if "@" in identifier:
        return get_user_by_email(identifier)
    user = get_user_by_username(identifier)
    if user:
        return user
    return get_user_by_email(identifier)


def get_user_by_id(user_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def create_user_record(name, email, password, company_name=None, username=None):
    with DB_LOCK:
        conn = get_connection()
        clean_username = (username or "").strip().lower()
        if clean_username:
            if not re.fullmatch(r"[a-z0-9_.-]{3,24}", clean_username):
                conn.close()
                raise ValueError("Username must be 3-24 characters (letters, numbers, . _ -).")
            if conn.execute("SELECT 1 FROM users WHERE LOWER(username) = ?", (clean_username,)).fetchone():
                conn.close()
                raise ValueError("That user ID is already taken.")
        else:
            clean_username = _generate_unique_username(conn, name, email)

        user = {
            "id": make_id("usr"),
            "name": name.strip() or "User",
            "email": email.strip().lower(),
            "username": clean_username,
            "password_hash": hash_password(password),
            "company_name": company_name,
            "created_at": now_iso(),
        }
        conn.execute(
            """
            INSERT INTO users (id, name, email, username, password_hash, company_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                user["name"],
                user["email"],
                user["username"],
                user["password_hash"],
                user["company_name"],
                user["created_at"],
            ),
        )
        conn.commit()
        conn.close()
    return user


def update_user_company_name(user_id, company_name):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "UPDATE users SET company_name = ? WHERE id = ?",
            (company_name.strip(), user_id),
        )
        conn.commit()
        conn.close()


def update_user_password(user_id, password_hash):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        conn.commit()
        conn.close()


def public_user(user):
    return {
        "id": user["id"],
        "name": user.get("name") or user.get("company_name") or "User",
        "email": user["email"],
        "username": user.get("username") or "",
        "company": user.get("company_name") or user.get("name") or "",
    }


def public_user_web(user):
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username") or "",
        "company": user.get("company_name") or user.get("name") or "",
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def create_session(user_id, ttl_hours=SESSION_TTL_HOURS):
    token = secrets.token_urlsafe(32)
    expires_at = (
        datetime.now() + timedelta(hours=ttl_hours)
        if ttl_hours is not None
        else datetime(2099, 12, 31)
    )
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO sessions (token, user_id, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (token, user_id, expires_at.isoformat(timespec="seconds"), now_iso()),
        )
        conn.commit()
        conn.close()
    return token


def get_session(token):
    if not token:
        return None

    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            conn.close()
            return None

        session = dict(row)
        expires_at = datetime.fromisoformat(session["expires_at"])
        if datetime.now() >= expires_at:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            conn.close()
            return None

        conn.close()
    return session


def delete_session(token):
    if not token:
        return
    with DB_LOCK:
        conn = get_connection()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()


def get_user_for_session_token(token):
    session = get_session(token)
    if not session:
        return None
    return get_user_by_id(session["user_id"])


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

def ensure_default_company(conn, user_id, company_name="My Company"):
    row = conn.execute(
        "SELECT id FROM companies WHERE user_id = ? ORDER BY created_at ASC LIMIT 1",
        (user_id,),
    ).fetchone()
    if row:
        return row[0]

    user = conn.execute("SELECT email, company_name FROM users WHERE id = ?", (user_id,)).fetchone()
    email = user[0] if user else ""
    name = (user[1] if user and user[1] else None) or company_name
    company_id = make_id("cmp")
    conn.execute(
        """
        INSERT INTO companies
        (id, user_id, name, email, receivers, alert_after_seconds, created_at)
        VALUES (?, ?, ?, ?, ?, 30, ?)
        """,
        (company_id, user_id, name, email, email, now_iso()),
    )
    return company_id


def get_default_company_id(user_id):
    with DB_LOCK:
        conn = get_connection()
        company_id = ensure_default_company(conn, user_id)
        conn.commit()
        conn.close()
    return company_id


def user_companies(user_id):
    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(
            "SELECT * FROM companies WHERE user_id = ? ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        conn.close()
    return [dict(row) for row in rows]


def find_company(user_id, company_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM companies WHERE id = ? AND user_id = ?",
            (company_id, user_id),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def find_company_by_id(company_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM companies WHERE id = ?",
            (company_id,),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def all_company_ids():
    """Every company/group id in the system, across every user account.
    Used by the background monitor so devices get checked on their own
    schedule regardless of whether anyone has the dashboard open."""
    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute("SELECT id FROM companies").fetchall()
        conn.close()
    return [row["id"] for row in rows]


def create_company(user_id, name, email, receivers, alert_after_seconds=30):
    company = {
        "id": make_id("cmp"),
        "user_id": user_id,
        "name": name,
        "email": email,
        "receivers": receivers,
        "alert_after_seconds": alert_after_seconds,
        "created_at": now_iso(),
    }
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO companies
            (id, user_id, name, email, receivers, alert_after_seconds, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company["id"],
                company["user_id"],
                company["name"],
                company["email"],
                company["receivers"],
                company["alert_after_seconds"],
                company["created_at"],
            ),
        )
        conn.commit()
        conn.close()
    return company


def update_company(company_id, name, email, receivers, alert_after_seconds, online_alerts=None):
    with DB_LOCK:
        conn = get_connection()
        if online_alerts is None:
            conn.execute(
                """
                UPDATE companies
                SET name = ?, email = ?, receivers = ?, alert_after_seconds = ?
                WHERE id = ?
                """,
                (name, email, receivers, alert_after_seconds, company_id),
            )
        else:
            conn.execute(
                """
                UPDATE companies
                SET name = ?, email = ?, receivers = ?, alert_after_seconds = ?, online_alerts = ?
                WHERE id = ?
                """,
                (name, email, receivers, alert_after_seconds, 1 if online_alerts else 0, company_id),
            )
        conn.commit()
        conn.close()


def delete_company(company_id):
    with DB_LOCK:
        conn = get_connection()
        conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def company_devices(company_id, user_id=None):
    query = "SELECT * FROM devices WHERE company_id = ?"
    params = [company_id]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)

    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(query, params).fetchall()
        conn.close()
    return [dict(row) for row in rows]


def get_device_for_user(device_id, user_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            """
            SELECT d.* FROM devices d
            JOIN companies c ON d.company_id = c.id
            WHERE d.id = ? AND c.user_id = ?
            """,
            (device_id, user_id),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def get_device_in_company(device_id, company_id, user_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM devices WHERE id = ? AND company_id = ? AND user_id = ?",
            (device_id, company_id, user_id),
        ).fetchone()
        conn.close()
    return dict(row) if row else None


def get_device_by_id(device_id):
    """Internal lookup used by the background monitoring loop (no user_id
    scoping needed -- the monitor already only knows about devices that
    exist). Returns None once a device has been deleted, which is how the
    per-device monitor thread knows to stop."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def create_device(device):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO devices
            (id, company_id, user_id, location, name, ip, muted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device["id"],
                device["company_id"],
                device["user_id"],
                device["location"],
                device["name"],
                device["ip"],
                device.get("muted", 0),
                device["created_at"],
            ),
        )
        conn.commit()
        conn.close()


def create_devices(devices):
    with DB_LOCK:
        conn = get_connection()
        for device in devices:
            conn.execute(
                """
                INSERT INTO devices
                (id, company_id, user_id, location, name, ip, muted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device["id"],
                    device["company_id"],
                    device["user_id"],
                    device["location"],
                    device["name"],
                    device["ip"],
                    device.get("muted", 0),
                    device["created_at"],
                ),
            )
        conn.commit()
        conn.close()


def update_device(device_id, company_id, user_id, location, name, ip, muted):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            UPDATE devices
            SET location = ?, name = ?, ip = ?, muted = ?
            WHERE id = ? AND company_id = ? AND user_id = ?
            """,
            (location, name, ip, muted, device_id, company_id, user_id),
        )
        conn.commit()
        conn.close()


def delete_device(device_id, company_id, user_id):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "DELETE FROM devices WHERE id = ? AND company_id = ? AND user_id = ?",
            (device_id, company_id, user_id),
        )
        conn.commit()
        conn.close()


def replace_user_devices(user_id, devices):
    company_id = get_default_company_id(user_id)
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "DELETE FROM devices WHERE company_id = ? AND user_id = ?",
            (company_id, user_id),
        )
        for device in devices:
            conn.execute(
                """
                INSERT INTO devices
                (id, company_id, user_id, location, name, ip, muted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    make_id("dev"),
                    company_id,
                    user_id,
                    device.get("location", "-") or "-",
                    device.get("name", "Unnamed Device") or "Unnamed Device",
                    device["ip"],
                    now_iso(),
                ),
            )
        conn.commit()
        conn.close()


def list_user_devices_ordered(user_id):
    company_id = get_default_company_id(user_id)
    devices = company_devices(company_id, user_id)
    return [
        {
            "id": index,
            "location": device["location"],
            "name": device["name"],
            "ip": device["ip"],
        }
        for index, device in enumerate(devices)
    ]


# ---------------------------------------------------------------------------
# Ping logs
# ---------------------------------------------------------------------------

def insert_ping_log(device_id, user_id, timestamp, status, latency, downtime):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO ping_logs (device_id, user_id, timestamp, status, latency, downtime)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (device_id, user_id, timestamp, status, latency, downtime),
        )
        conn.commit()
        conn.close()


def insert_minor_interruption(record):
    """Persist one Minor Interruption (confirmed drop that recovered before
    the Verified Outage threshold, or one caused by our own internet, or one
    a user manually moved down from Major) for the Minor incident log and
    Network Fluctuation detection. Never touches reports/outage_history/
    availability directly (see reclassify_minor_to_major for the one
    deliberate, user-triggered exception)."""
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO minor_interruptions
            (id, device_id, user_id, company_id, device_name, ip_address,
             location, start_time, end_time, duration_seconds, cause, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record["id"],
                record["device_id"],
                record.get("user_id"),
                record.get("company_id"),
                record.get("device_name"),
                record.get("ip_address"),
                record.get("location"),
                record["start_time"],
                record["end_time"],
                record["duration_seconds"],
                record.get("cause") or "short",
                record.get("created_at") or now_iso(),
            ),
        )
        conn.commit()
        conn.close()


def count_recent_minor_interruptions(device_id, since_iso):
    """Count Minor Interruptions for a device with start_time >= since_iso.
    Used for the rolling-window Network Fluctuation check."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM minor_interruptions WHERE device_id = ? AND start_time >= ?",
            (device_id, since_iso),
        ).fetchone()
        conn.close()
    return row["c"] if row else 0


def prune_old_minor_interruptions(retention_days=2):
    """Minor Interruptions only matter for a short rolling window (fluctuation
    detection), so old rows are pruned periodically to keep the table small."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat(timespec="seconds")
    with DB_LOCK:
        conn = get_connection()
        conn.execute("DELETE FROM minor_interruptions WHERE start_time < ?", (cutoff,))
        conn.commit()
        conn.close()


def list_minor_interruptions(company_id, device_id=None, start_date=None, end_date=None):
    """Minor incident log: every confirmed drop that was classified Minor
    (short blip, our-own-internet outage, or manually moved down from
    Major), shaped like company_reports() so the frontend can render both
    with the same row template. `start_date`/`end_date` are 'YYYY-MM-DD'."""
    query = "SELECT * FROM minor_interruptions WHERE company_id = ?"
    params = [company_id]
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    if start_date:
        query += " AND start_time >= ?"
        params.append(f"{start_date}T00:00:00")
    if end_date:
        query += " AND start_time <= ?"
        params.append(f"{end_date}T23:59:59")

    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(query + " ORDER BY start_time DESC", params).fetchall()
        conn.close()

    results = []
    for row in rows:
        m = dict(row)
        try:
            start_dt = datetime.fromisoformat(m["start_time"])
            end_dt = datetime.fromisoformat(m["end_time"])
        except Exception:
            continue
        duration = m.get("duration_seconds") or 0
        results.append({
            "id": m["id"],
            "device_id": m["device_id"],
            "company_id": m.get("company_id"),
            "date": start_dt.strftime("%d-%m-%Y"),
            "date_iso": start_dt.strftime("%Y-%m-%d"),
            "location": m.get("location"),
            "name": m.get("device_name"),
            "ip": m.get("ip_address"),
            "offline": start_dt.strftime("%H:%M:%S"),
            "online": end_dt.strftime("%H:%M:%S"),
            "downtime": str(timedelta(seconds=int(round(duration)))),
            "duration_seconds": duration,
            "cause": m.get("cause") or "short",
        })
    return results


def get_report_by_id(report_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def get_minor_interruption_by_id(minor_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM minor_interruptions WHERE id = ?", (minor_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def get_device_ping_logs(device_id, user_id, start_iso, end_iso):
    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT timestamp, status, latency, downtime FROM ping_logs
            WHERE device_id = ? AND user_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (device_id, user_id, start_iso, end_iso),
        ).fetchall()
        conn.close()
    return [dict(row) for row in rows]


def delete_device_ping_logs(device_id):
    with DB_LOCK:
        conn = get_connection()
        conn.execute("DELETE FROM ping_logs WHERE device_id = ?", (device_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Retention: roll old raw ping logs into a permanent per-day summary, then
# delete the raw rows to keep the database small. Reports/exports read the
# summary transparently once raw rows for a date are gone.
# ---------------------------------------------------------------------------

def _parse_latency_ms(latency_str):
    if not latency_str:
        return None
    text = str(latency_str).strip().lower()
    if text in ("--", "-", ""):
        return None
    if text.startswith("<1"):
        return 0.5
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _extract_outage_periods(rows):
    """rows: ordered list of dicts with timestamp (ISO) and status.
    Returns a list of {start, end, duration_seconds} — one entry per
    contiguous OFFLINE streak (i.e. one entry per actual outage, as opposed
    to `sessions` which track contiguous monitoring runs). An outage still
    in progress at the end of the given rows is closed at the last seen
    timestamp so duration is never left unset."""
    periods = []
    streak_start = None
    prev_ts_iso = None
    for row in rows:
        if row["status"] == "OFFLINE":
            if streak_start is None:
                streak_start = row["timestamp"]
            prev_ts_iso = row["timestamp"]
        else:
            if streak_start is not None:
                start_dt = datetime.fromisoformat(streak_start)
                end_dt = datetime.fromisoformat(row["timestamp"])
                periods.append({
                    "start": streak_start,
                    "end": row["timestamp"],
                    "duration_seconds": round((end_dt - start_dt).total_seconds(), 1),
                })
                streak_start = None
    if streak_start is not None and prev_ts_iso is not None:
        start_dt = datetime.fromisoformat(streak_start)
        end_dt = datetime.fromisoformat(prev_ts_iso)
        periods.append({
            "start": streak_start,
            "end": prev_ts_iso,
            "duration_seconds": round((end_dt - start_dt).total_seconds(), 1),
        })
    return periods


def _compute_day_stats_from_rows(rows, session_gap_minutes=15):
    """rows: list of dicts with timestamp (ISO), status, latency, ordered ASC.
    Returns (total_checks, successful, failed, avg_latency_ms, total_outage_seconds, sessions, extra)
    where sessions is a list of {start, end, total_checks, successful, failed,
    avg_latency_ms, outage_seconds} — one entry per contiguous run of pings
    (a "session" = the site/monitor was open and pinging continuously) — and
    extra is a dict of the additional long-term stats (min/max latency,
    outage_count, longest/shortest outage, outage_periods, uptime %).
    """
    if not rows:
        empty_extra = {
            "minimum_latency_ms": None, "maximum_latency_ms": None,
            "outage_count": 0, "longest_outage_seconds": None,
            "shortest_outage_seconds": None, "outage_periods": [],
            "uptime_percentage": None, "downtime_percentage": None,
        }
        return 0, 0, 0, None, 0.0, [], empty_extra

    total = len(rows)
    successful = sum(1 for r in rows if r["status"] == "ONLINE")
    failed = total - successful
    latencies = [v for v in (_parse_latency_ms(r["latency"]) for r in rows) if v is not None]
    avg_latency = round(sum(latencies) / len(latencies), 1) if latencies else None
    min_latency = round(min(latencies), 1) if latencies else None
    max_latency = round(max(latencies), 1) if latencies else None

    outage_periods = _extract_outage_periods(rows)
    durations = [p["duration_seconds"] for p in outage_periods]
    extra = {
        "minimum_latency_ms": min_latency,
        "maximum_latency_ms": max_latency,
        "outage_count": len(outage_periods),
        "longest_outage_seconds": max(durations) if durations else None,
        "shortest_outage_seconds": min(durations) if durations else None,
        "outage_periods": outage_periods,
        "uptime_percentage": round((successful / total) * 100, 2) if total else None,
        "downtime_percentage": round((failed / total) * 100, 2) if total else None,
    }

    gap = timedelta(minutes=session_gap_minutes)
    sessions = []
    current = [rows[0]]
    prev_ts = datetime.fromisoformat(rows[0]["timestamp"])

    def close_session(chunk):
        c_total = len(chunk)
        c_success = sum(1 for r in chunk if r["status"] == "ONLINE")
        c_failed = c_total - c_success
        c_lat = [v for v in (_parse_latency_ms(r["latency"]) for r in chunk) if v is not None]
        c_avg = round(sum(c_lat) / len(c_lat), 1) if c_lat else None
        outage_seconds = _sum_outage_seconds(chunk)
        return {
            "start": chunk[0]["timestamp"],
            "end": chunk[-1]["timestamp"],
            "total_checks": c_total,
            "successful": c_success,
            "failed": c_failed,
            "avg_latency_ms": c_avg,
            "outage_seconds": outage_seconds,
        }

    for row in rows[1:]:
        ts = datetime.fromisoformat(row["timestamp"])
        if ts - prev_ts > gap:
            sessions.append(close_session(current))
            current = []
        current.append(row)
        prev_ts = ts
    sessions.append(close_session(current))

    total_outage_seconds = _sum_outage_seconds(rows)
    return total, successful, failed, avg_latency, total_outage_seconds, sessions, extra


def _sum_outage_seconds(rows):
    """Sum durations of OFFLINE streaks within an ordered list of rows.
    A streak's duration is measured from its first OFFLINE timestamp to the
    following ONLINE timestamp (recovery), or to the streak's own last
    timestamp if it never recovers within the given rows (still down)."""
    total_seconds = 0.0
    streak_start = None
    prev_ts = None
    for row in rows:
        ts = datetime.fromisoformat(row["timestamp"])
        if row["status"] == "OFFLINE":
            if streak_start is None:
                streak_start = ts
            prev_ts = ts
        else:
            if streak_start is not None:
                total_seconds += (ts - streak_start).total_seconds()
                streak_start = None
            prev_ts = ts
    if streak_start is not None and prev_ts is not None:
        total_seconds += (prev_ts - streak_start).total_seconds()
    return round(total_seconds, 1)


def get_daily_summary(device_id, date):
    """date: 'YYYY-MM-DD'. Returns the stored rollup row, or None if this day
    hasn't been rolled up yet (e.g. it's recent/still within the retention
    window and raw logs are still present)."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM daily_summary WHERE device_id = ? AND date = ?",
            (device_id, date),
        ).fetchone()
        conn.close()
    if not row:
        return None
    data = dict(row)
    data["sessions"] = json.loads(data.get("sessions_json") or "[]")
    data["outage_periods"] = json.loads(data.get("outage_periods_json") or "[]")
    return data


def get_day_stats(device_id, user_id, date, session_gap_minutes=15):
    """Unified accessor used by exports: returns per-day stats whether the
    raw logs still exist (computes on the fly) or have already been rolled up
    and pruned (reads the saved summary). Either way the caller gets the same
    shape, including a session breakdown.
    """
    summary = get_daily_summary(device_id, date)
    if summary:
        return {
            "date": date,
            "total_checks": summary["total_checks"],
            "successful": summary["successful_checks"],
            "failed": summary["failed_checks"],
            "avg_latency_ms": summary["avg_latency_ms"],
            "outage_seconds": summary["total_outage_seconds"],
            "sessions": summary["sessions"],
            "source": "rollup",
            # Extended long-term stats (may be None for days rolled up before
            # this field set existed — see migrate_add_daily_summary_extended_stats).
            "minimum_latency_ms": summary.get("minimum_latency_ms"),
            "maximum_latency_ms": summary.get("maximum_latency_ms"),
            "outage_count": summary.get("outage_count"),
            "longest_outage_seconds": summary.get("longest_outage_seconds"),
            "shortest_outage_seconds": summary.get("shortest_outage_seconds"),
            "uptime_percentage": summary.get("uptime_percentage"),
            "downtime_percentage": summary.get("downtime_percentage"),
        }

    start_iso = f"{date}T00:00:00"
    end_iso = f"{date}T23:59:59"
    rows = get_device_ping_logs(device_id, user_id, start_iso, end_iso)
    total, successful, failed, avg_latency, outage_seconds, sessions, extra = _compute_day_stats_from_rows(
        rows, session_gap_minutes=session_gap_minutes
    )
    return {
        "date": date,
        "total_checks": total,
        "successful": successful,
        "failed": failed,
        "avg_latency_ms": avg_latency,
        "outage_seconds": outage_seconds,
        "sessions": sessions,
        "source": "raw",
        "minimum_latency_ms": extra["minimum_latency_ms"],
        "maximum_latency_ms": extra["maximum_latency_ms"],
        "outage_count": extra["outage_count"],
        "longest_outage_seconds": extra["longest_outage_seconds"],
        "shortest_outage_seconds": extra["shortest_outage_seconds"],
        "uptime_percentage": extra["uptime_percentage"],
        "downtime_percentage": extra["downtime_percentage"],
    }


def vacuum_database():
    """Reclaim disk space freed by the daily ping_logs pruning. VACUUM cannot
    run inside a transaction, so this uses its own connection in autocommit
    mode. Safe to call periodically (e.g. weekly) from a background worker."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_FILE, timeout=30.0, isolation_level=None)
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()


def rollup_and_prune_old_ping_logs(retention_days=7, session_gap_minutes=15):
    """Once a day: for any calendar date older than `retention_days`, compute
    and permanently save a daily_summary row per device (if not already
    saved), then delete that date's raw ping_logs rows. Safe to call
    repeatedly (e.g. every few hours) — already-summarized dates are skipped.
    """
    cutoff_date = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    with DB_LOCK:
        conn = get_connection()
        old_dates = [
            r["d"] for r in conn.execute(
                "SELECT DISTINCT substr(timestamp, 1, 10) AS d FROM ping_logs WHERE substr(timestamp, 1, 10) < ?",
                (cutoff_date,),
            ).fetchall()
        ]
        conn.close()

    for date in old_dates:
        with DB_LOCK:
            conn = get_connection()
            device_user_pairs = conn.execute(
                """
                SELECT DISTINCT device_id, user_id FROM ping_logs
                WHERE substr(timestamp, 1, 10) = ?
                """,
                (date,),
            ).fetchall()
            conn.close()

        for pair in device_user_pairs:
            device_id, user_id = pair["device_id"], pair["user_id"]
            existing = get_daily_summary(device_id, date)
            if existing is None:
                rows = get_device_ping_logs(device_id, user_id, f"{date}T00:00:00", f"{date}T23:59:59")
                total, successful, failed, avg_latency, outage_seconds, sessions, extra = _compute_day_stats_from_rows(
                    rows, session_gap_minutes=session_gap_minutes
                )
                total_downtime_minutes = round(outage_seconds / 60, 2) if outage_seconds else 0.0
                with DB_LOCK:
                    conn = get_connection()
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO daily_summary
                        (id, device_id, user_id, date, total_checks, successful_checks,
                         failed_checks, avg_latency_ms, total_outage_seconds, sessions_json,
                         minimum_latency_ms, maximum_latency_ms, outage_count,
                         longest_outage_seconds, shortest_outage_seconds, outage_periods_json,
                         total_downtime_minutes, uptime_percentage, downtime_percentage, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            make_id("sum"), device_id, user_id, date, total, successful,
                            failed, avg_latency, outage_seconds, json.dumps(sessions),
                            extra["minimum_latency_ms"], extra["maximum_latency_ms"], extra["outage_count"],
                            extra["longest_outage_seconds"], extra["shortest_outage_seconds"],
                            json.dumps(extra["outage_periods"]), total_downtime_minutes,
                            extra["uptime_percentage"], extra["downtime_percentage"], now_iso(),
                        ),
                    )
                    conn.commit()
                    conn.close()

        # All devices for this date are now safely summarized — prune raw rows.
        with DB_LOCK:
            conn = get_connection()
            conn.execute(
                "DELETE FROM ping_logs WHERE substr(timestamp, 1, 10) = ?",
                (date,),
            )
            conn.commit()
            conn.close()

    if old_dates:
        print(f"[retention] Rolled up and pruned raw ping logs for {len(old_dates)} day(s) older than {retention_days} days.")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def company_reports(company_id, user_id=None, date_iso=None, start_date=None, end_date=None):
    query = "SELECT * FROM reports WHERE company_id = ?"
    params = [company_id]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    if date_iso:
        query += " AND date_iso = ?"
        params.append(date_iso)
    elif start_date and end_date:
        query += " AND date_iso >= ? AND date_iso <= ?"
        params.extend([start_date, end_date])
    elif start_date:
        query += " AND date_iso >= ?"
        params.append(start_date)
    elif end_date:
        query += " AND date_iso <= ?"
        params.append(end_date)

    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(
            query + " ORDER BY created_at DESC",
            params,
        ).fetchall()
        conn.close()
    return [dict(row) for row in rows]


def user_reports(user_id, ip_filter=None):
    with DB_LOCK:
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT date, location, name, ip, offline, online, downtime
            FROM reports WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()
        conn.close()

    reports = [dict(row) for row in rows]
    if ip_filter and ip_filter != "all":
        reports = [report for report in reports if report["ip"] == ip_filter]
    return reports


def _date_str_to_iso(date_str):
    """Convert the 'DD-MM-YYYY' display date used in reports to 'YYYY-MM-DD'."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _downtime_str_to_seconds(downtime_str):
    """Best-effort parse of the display duration string (e.g. '0:02:35' or
    '1 day, 3:04:05', the str(timedelta) format) back into whole seconds.
    Used only as a fallback when precise ISO timestamps aren't available."""
    if not downtime_str:
        return 0
    text = str(downtime_str).strip()
    days = 0
    if "," in text:
        day_part, text = text.split(",", 1)
        text = text.strip()
        match = re.search(r"(\d+)", day_part)
        days = int(match.group(1)) if match else 0
    try:
        h, m, s = text.split(":")
        return days * 86400 + int(h) * 3600 + int(m) * 60 + int(float(s))
    except ValueError:
        return 0


def _report_iso_window(report):
    """Reconstruct (offline_start_iso, online_iso, duration_seconds) for a
    report. The live monitor's in-memory report dict carries the precise ISO
    columns directly; a report dict loaded back from the `reports` table
    (which doesn't persist them) gets them rebuilt from the display date/time
    strings instead -- handling the case where the outage crossed midnight."""
    offline_start_iso = report.get("offline_start_iso")
    online_iso = report.get("online_iso")
    duration_seconds = report.get("duration_seconds")

    date_iso = report.get("date_iso") or _date_str_to_iso(report.get("date"))
    if not offline_start_iso and date_iso and report.get("offline"):
        offline_start_iso = f"{date_iso}T{report['offline']}"
    if not online_iso and date_iso and report.get("online"):
        # Best-effort: assume same day unless the online time is earlier than
        # the offline time, which implies the outage crossed midnight.
        online_iso = f"{date_iso}T{report['online']}"
        if offline_start_iso and online_iso < offline_start_iso:
            next_day = (datetime.fromisoformat(date_iso) + timedelta(days=1)).strftime("%Y-%m-%d")
            online_iso = f"{next_day}T{report['online']}"
    if duration_seconds is None:
        duration_seconds = _downtime_str_to_seconds(report.get("downtime"))
    return offline_start_iso, online_iso, duration_seconds


def _record_outage_history(conn, report):
    """Write one permanent outage_history row from a recovered-outage report
    dict. Uses precise ISO timestamps (offline_start_iso/online_iso) when the
    caller provides them (the live monitoring loop always does); otherwise
    falls back to reconstructing from the display date/time strings."""
    offline_start_iso, online_iso, duration_seconds = _report_iso_window(report)
    date_iso = report.get("date_iso") or _date_str_to_iso(report.get("date"))

    if not offline_start_iso or not online_iso:
        return  # not enough information to record a permanent entry

    conn.execute(
        """
        INSERT OR IGNORE INTO outage_history
        (outage_id, user_id, company_id, device_id, device_name, ip_address,
         location, offline_start, online_time, outage_duration_seconds,
         outage_duration_readable, date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("out"),
            report.get("user_id"),
            report.get("company_id"),
            report.get("device_id"),
            report.get("name"),
            report.get("ip"),
            report.get("location"),
            offline_start_iso,
            online_iso,
            int(round(duration_seconds)),
            report.get("downtime"),
            date_iso,
            now_iso(),
        ),
    )


def insert_report(report):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO reports
            (id, company_id, device_id, user_id, date, date_iso, location, name, ip,
             offline, online, downtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["id"],
                report["company_id"],
                report["device_id"],
                report["user_id"],
                report.get("date"),
                report.get("date_iso") or _date_str_to_iso(report.get("date")),
                report.get("location"),
                report.get("name"),
                report.get("ip"),
                report.get("offline"),
                report.get("online"),
                report.get("downtime"),
                report.get("created_at"),
            ),
        )
        _record_outage_history(conn, report)
        conn.commit()
        conn.close()


def insert_reports(reports):
    with DB_LOCK:
        conn = get_connection()
        for report in reports:
            conn.execute(
                """
                INSERT INTO reports
                (id, company_id, device_id, user_id, date, date_iso, location, name, ip,
                 offline, online, downtime, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["id"],
                    report["company_id"],
                    report["device_id"],
                    report["user_id"],
                    report.get("date"),
                    report.get("date_iso") or _date_str_to_iso(report.get("date")),
                    report.get("location"),
                    report.get("name"),
                    report.get("ip"),
                    report.get("offline"),
                    report.get("online"),
                    report.get("downtime"),
                    report.get("created_at"),
                ),
            )
            _record_outage_history(conn, report)
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Manual Minor <-> Major reclassification (Incident Log feature).
#
# Major incidents live in `reports` (+ a permanent `outage_history` ledger
# entry that is intentionally left untouched by reclassification) and are
# bracketed by exactly two ping_logs rows (OFFLINE at start, ONLINE at
# recovery) -- those bracket rows are what day-stats/bar-charts/Excel
# exports actually read, so moving an incident between tables always keeps
# those two rows, and the daily rollup, in sync with the new classification.
# ---------------------------------------------------------------------------

def reclassify_minor_to_major(minor_id):
    """Promote a Minor Interruption to a Verified Outage (Major): moves the
    row into reports/outage_history and adds the matching OFFLINE/ONLINE
    bracket rows to ping_logs, mirroring exactly what the live monitor does
    automatically when a real outage crosses the threshold on its own."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM minor_interruptions WHERE id = ?", (minor_id,)).fetchone()
        if not row:
            conn.close()
            return None
        m = dict(row)

        offline_start_iso = m["start_time"]
        online_iso = m["end_time"]
        duration_seconds = m.get("duration_seconds") or 0
        downtime_display = str(timedelta(seconds=int(round(duration_seconds))))
        offline_dt = datetime.fromisoformat(offline_start_iso)
        online_dt = datetime.fromisoformat(online_iso)

        report = {
            "id": make_id("rep"),
            "company_id": m.get("company_id"),
            "device_id": m["device_id"],
            "user_id": m.get("user_id"),
            "date": offline_dt.strftime("%d-%m-%Y"),
            "location": m.get("location"),
            "name": m.get("device_name"),
            "ip": m.get("ip_address"),
            "offline": offline_dt.strftime("%H:%M:%S"),
            "online": online_dt.strftime("%H:%M:%S"),
            "downtime": downtime_display,
            "created_at": now_iso(),
        }

        conn.execute(
            """
            INSERT INTO reports
            (id, company_id, device_id, user_id, date, date_iso, location, name, ip,
             offline, online, downtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["id"], report["company_id"], report["device_id"], report["user_id"],
                report["date"], _date_str_to_iso(report["date"]), report["location"], report["name"],
                report["ip"], report["offline"], report["online"], report["downtime"], report["created_at"],
            ),
        )
        _record_outage_history(conn, {
            **report,
            "offline_start_iso": offline_start_iso,
            "online_iso": online_iso,
            "duration_seconds": duration_seconds,
        })

        # Add the two bracket rows so day-stats/exports now count this outage.
        conn.execute(
            "INSERT INTO ping_logs (device_id, user_id, timestamp, status, latency, downtime) "
            "VALUES (?, ?, ?, 'OFFLINE', '--', '-')",
            (m["device_id"], m.get("user_id"), offline_start_iso),
        )
        conn.execute(
            "INSERT INTO ping_logs (device_id, user_id, timestamp, status, latency, downtime) "
            "VALUES (?, ?, ?, 'ONLINE', '--', ?)",
            (m["device_id"], m.get("user_id"), online_iso, downtime_display),
        )

        conn.execute("DELETE FROM minor_interruptions WHERE id = ?", (minor_id,))

        # If that day was already rolled up into a summary, drop the stale
        # summary so get_day_stats recomputes it fresh (from raw ping_logs,
        # which still hold the two rows we just inserted above).
        conn.execute(
            "DELETE FROM daily_summary WHERE device_id = ? AND date = ?",
            (m["device_id"], offline_start_iso[:10]),
        )

        conn.commit()
        conn.close()
    return report


def reclassify_report_to_minor(report_id):
    """Demote a Verified Outage (Major) back to a Minor Interruption: moves
    the row into minor_interruptions and removes its two OFFLINE/ONLINE
    bracket rows from ping_logs, so day-stats/exports stop counting it.
    The permanent outage_history ledger entry is deliberately left as-is --
    it's an audit trail of what actually happened, independent of how the
    incident is currently classified in the log."""
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            conn.close()
            return None
        r = dict(row)
        offline_start_iso, online_iso, duration_seconds = _report_iso_window(r)

        minor = {
            "id": make_id("mint"),
            "device_id": r["device_id"],
            "user_id": r.get("user_id"),
            "company_id": r.get("company_id"),
            "device_name": r.get("name"),
            "ip_address": r.get("ip"),
            "location": r.get("location"),
            "start_time": offline_start_iso or f"{r.get('date_iso')}T00:00:00",
            "end_time": online_iso or offline_start_iso or f"{r.get('date_iso')}T00:00:00",
            "duration_seconds": round(duration_seconds or 0, 1),
            "cause": "manual",
            "created_at": now_iso(),
        }

        conn.execute(
            """
            INSERT INTO minor_interruptions
            (id, device_id, user_id, company_id, device_name, ip_address, location,
             start_time, end_time, duration_seconds, cause, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                minor["id"], minor["device_id"], minor["user_id"], minor["company_id"],
                minor["device_name"], minor["ip_address"], minor["location"],
                minor["start_time"], minor["end_time"], minor["duration_seconds"],
                minor["cause"], minor["created_at"],
            ),
        )

        conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))

        if offline_start_iso:
            conn.execute(
                "DELETE FROM ping_logs WHERE device_id = ? AND timestamp = ? AND status = 'OFFLINE'",
                (r["device_id"], offline_start_iso),
            )
        if online_iso:
            conn.execute(
                "DELETE FROM ping_logs WHERE device_id = ? AND timestamp = ? AND status = 'ONLINE'",
                (r["device_id"], online_iso),
            )

        date_str = r.get("date_iso") or _date_str_to_iso(r.get("date")) or (offline_start_iso or "")[:10] or None
        if date_str:
            conn.execute(
                "DELETE FROM daily_summary WHERE device_id = ? AND date = ?",
                (r["device_id"], date_str),
            )

        conn.commit()
        conn.close()
    return minor


def append_user_outage_report(user_id, location, name, ip, offline_time, online_time, downtime):
    company_id = get_default_company_id(user_id)
    with DB_LOCK:
        conn = get_connection()
        device = conn.execute(
            "SELECT id FROM devices WHERE company_id = ? AND user_id = ? AND ip = ?",
            (company_id, user_id, ip),
        ).fetchone()
        device_id = device[0] if device else make_id("dev")
        if not device:
            conn.execute(
                """
                INSERT INTO devices
                (id, company_id, user_id, location, name, ip, muted, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (device_id, company_id, user_id, location or "-", name or "Unknown", ip, now_iso()),
            )
        conn.execute(
            """
            INSERT INTO reports
            (id, company_id, device_id, user_id, date, date_iso, location, name, ip,
             offline, online, downtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("rep"),
                company_id,
                device_id,
                user_id,
                offline_time.strftime("%d-%m-%Y"),
                offline_time.strftime("%Y-%m-%d"),
                location,
                name,
                ip,
                offline_time.strftime("%H:%M:%S"),
                online_time.strftime("%H:%M:%S"),
                downtime,
                now_iso(),
            ),
        )
        _record_outage_history(conn, {
            "user_id": user_id,
            "company_id": company_id,
            "device_id": device_id,
            "name": name,
            "ip": ip,
            "location": location,
            "offline_start_iso": offline_time.isoformat(timespec="seconds"),
            "online_iso": online_time.isoformat(timespec="seconds"),
            "duration_seconds": (online_time - offline_time).total_seconds(),
            "downtime": downtime,
            "date": offline_time.strftime("%d-%m-%Y"),
        })
        conn.commit()
        conn.close()


def delete_device_reports(device_id, company_id, user_id):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            "DELETE FROM reports WHERE device_id = ? AND company_id = ? AND user_id = ?",
            (device_id, company_id, user_id),
        )
        conn.commit()
        conn.close()


def delete_company_reports(company_id):
    with DB_LOCK:
        conn = get_connection()
        conn.execute("DELETE FROM reports WHERE company_id = ?", (company_id,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def ensure_user_settings(conn, user_id):
    conn.execute(
        """
        INSERT OR IGNORE INTO settings (user_id, timezone, refresh_interval)
        VALUES (?, ?, ?)
        """,
        (user_id, DEFAULT_SETTINGS["timezone"], DEFAULT_SETTINGS["refresh_interval"]),
    )


def get_user_settings(user_id):
    with DB_LOCK:
        conn = get_connection()
        ensure_user_settings(conn, user_id)
        row = conn.execute(
            "SELECT timezone, refresh_interval FROM settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        conn.commit()
        conn.close()
    settings = dict(row) if row else DEFAULT_SETTINGS.copy()
    return settings


def save_user_settings(user_id, timezone, refresh_interval):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO settings (user_id, timezone, refresh_interval)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                timezone = excluded.timezone,
                refresh_interval = excluded.refresh_interval
            """,
            (user_id, timezone, refresh_interval),
        )
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def ensure_user_alerts(conn, user_id, company_id=None):
    if company_id is None:
        company_id = ensure_default_company(conn, user_id)
    conn.execute(
        """
        INSERT OR IGNORE INTO alerts
        (id, user_id, company_id, primary_email, additional_emails,
         long_outage_alert_seconds, send_short_outage_reports)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            make_id("alr"),
            user_id,
            company_id,
            "",
            "",
            30,
            1,
        ),
    )


def get_user_alerts(user_id):
    with DB_LOCK:
        conn = get_connection()
        company_id = ensure_default_company(conn, user_id)
        ensure_user_alerts(conn, user_id, company_id)
        row = conn.execute(
            """
            SELECT primary_email, additional_emails, long_outage_alert_seconds,
                   send_short_outage_reports
            FROM alerts WHERE user_id = ? AND company_id = ?
            """,
            (user_id, company_id),
        ).fetchone()
        conn.commit()
        conn.close()

    if not row:
        return DEFAULT_ALERTS.copy()

    return {
        "primary_email": row["primary_email"],
        "additional_emails": row["additional_emails"],
        "long_outage_alert_seconds": row["long_outage_alert_seconds"],
        "send_short_outage_reports": bool(row["send_short_outage_reports"]),
    }


def save_user_alerts(user_id, alerts):
    with DB_LOCK:
        conn = get_connection()
        company_id = ensure_default_company(conn, user_id)
        ensure_user_alerts(conn, user_id, company_id)
        conn.execute(
            """
            UPDATE alerts
            SET primary_email = ?, additional_emails = ?,
                long_outage_alert_seconds = ?, send_short_outage_reports = ?
            WHERE user_id = ? AND company_id = ?
            """,
            (
                alerts.get("primary_email", ""),
                alerts.get("additional_emails", ""),
                alerts.get("long_outage_alert_seconds", 30),
                1 if alerts.get("send_short_outage_reports", True) else 0,
                user_id,
                company_id,
            ),
        )
        conn.commit()
        conn.close()


def bootstrap_user_data(user_id, company_name="My Company"):
    with DB_LOCK:
        conn = get_connection()
        ensure_default_company(conn, user_id, company_name)
        ensure_user_settings(conn, user_id)
        ensure_user_alerts(conn, user_id)
        conn.commit()
        conn.close()
