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
DATA_DIR = BASE_DIR / "data"
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
            CREATE INDEX IF NOT EXISTS idx_ping_logs_device_ts ON ping_logs(device_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_ping_logs_user_id ON ping_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_reports_company_id ON reports(company_id);
            CREATE INDEX IF NOT EXISTS idx_reports_user_id ON reports(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
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
# Users
# ---------------------------------------------------------------------------

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


def get_user_by_id(user_id):
    with DB_LOCK:
        conn = get_connection()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
    return dict(row) if row else None


def create_user_record(name, email, password, company_name=None):
    user = {
        "id": make_id("usr"),
        "name": name.strip() or "User",
        "email": email.strip().lower(),
        "password_hash": hash_password(password),
        "company_name": company_name,
        "created_at": now_iso(),
    }
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO users (id, name, email, password_hash, company_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user["id"],
                user["name"],
                user["email"],
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
        "company": user.get("company_name") or user.get("name") or "",
    }


def public_user_web(user):
    return {
        "id": user["id"],
        "email": user["email"],
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


def update_company(company_id, name, email, receivers, alert_after_seconds):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            UPDATE companies
            SET name = ?, email = ?, receivers = ?, alert_after_seconds = ?
            WHERE id = ?
            """,
            (name, email, receivers, alert_after_seconds, company_id),
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
# Reports
# ---------------------------------------------------------------------------

def company_reports(company_id, user_id=None):
    query = "SELECT * FROM reports WHERE company_id = ?"
    params = [company_id]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)

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


def insert_report(report):
    with DB_LOCK:
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO reports
            (id, company_id, device_id, user_id, date, location, name, ip,
             offline, online, downtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report["id"],
                report["company_id"],
                report["device_id"],
                report["user_id"],
                report.get("date"),
                report.get("location"),
                report.get("name"),
                report.get("ip"),
                report.get("offline"),
                report.get("online"),
                report.get("downtime"),
                report.get("created_at"),
            ),
        )
        conn.commit()
        conn.close()


def insert_reports(reports):
    with DB_LOCK:
        conn = get_connection()
        for report in reports:
            conn.execute(
                """
                INSERT INTO reports
                (id, company_id, device_id, user_id, date, location, name, ip,
                 offline, online, downtime, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report["id"],
                    report["company_id"],
                    report["device_id"],
                    report["user_id"],
                    report.get("date"),
                    report.get("location"),
                    report.get("name"),
                    report.get("ip"),
                    report.get("offline"),
                    report.get("online"),
                    report.get("downtime"),
                    report.get("created_at"),
                ),
            )
        conn.commit()
        conn.close()


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
            (id, company_id, device_id, user_id, date, location, name, ip,
             offline, online, downtime, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                make_id("rep"),
                company_id,
                device_id,
                user_id,
                offline_time.strftime("%d-%m-%Y"),
                location,
                name,
                ip,
                offline_time.strftime("%H:%M:%S"),
                online_time.strftime("%H:%M:%S"),
                downtime,
                now_iso(),
            ),
        )
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
