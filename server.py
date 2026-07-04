import csv
import json
import mimetypes
import os
import re
import subprocess
import threading
import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

# openpyxl for Excel reporting
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from openpyxl.utils import get_column_letter

# Imported email alert engine connection
import email_alerts
import database

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

HOST = "0.0.0.0"  # Adjusted to allow connections from other devices seamlessly
PORT = 8080

PING_TIMEOUT_SECONDS = 1
OFFLINE_AFTER_FAILURES = 3
ONLINE_AFTER_SUCCESSES = 1
MAX_WORKERS = 30

STATE_LOCK = threading.Lock()
MONITOR_STATE = {}


def now_iso():
    return database.now_iso()


def make_id(prefix):
    return database.make_id(prefix)


def hash_password(password):
    return database.hash_password(password)


def verify_password(password, stored_hash):
    return database.verify_password(password, stored_hash)


def get_user_by_email(email):
    return database.get_user_by_email(email)


def public_user(user):
    return database.public_user(user)


def get_session_user(handler):
    cookie_header = handler.headers.get("Cookie", "")
    cookie = SimpleCookie(cookie_header)
    token_cookie = cookie.get("netwatch_session")

    if token_cookie:
        return database.get_user_for_session_token(token_cookie.value)

    return None


def user_companies(user_id):
    return database.user_companies(user_id)


def find_company(user_id, company_id):
    return database.find_company(user_id, company_id)


def company_devices(company_id):
    return database.company_devices(company_id)


def company_reports(company_id):
    return database.company_reports(company_id)


def clean_device(data, company_id, user_id):
    location = str(data.get("location", "")).strip()
    name = str(data.get("name", "")).strip()
    ip = str(data.get("ip", "")).strip()
    muted = 1 if data.get("muted") in (True, 1, "1", "true", "True") else 0

    if not ip:
        raise ValueError("IP address is required")

    return {
        "id": data.get("id") or make_id("dev"),
        "company_id": company_id,
        "user_id": user_id,
        "location": location or "-",
        "name": name or "Unnamed Device",
        "ip": ip,
        "muted": muted,
        "created_at": data.get("created_at") or now_iso(),
    }


def build_ping_command(ip):
    if os.name == "nt":
        return ["ping", "-n", "1", "-w", str(PING_TIMEOUT_SECONDS * 1000), ip]
    return ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), ip]


def parse_ping_time(output):
    match = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", output, re.IGNORECASE)
    if not match:
        return "--"
    if "time<" in match.group(0).lower():
        return "<1 ms"
    return f"{match.group(1)} ms"


def ping_device(device):
    try:
        result = subprocess.run(
            build_ping_command(device["ip"]),
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT_SECONDS + 1,
        )
    except Exception:
        return {**device, "raw_status": "OFFLINE", "ping": "--"}

    raw_status = "ONLINE" if result.returncode == 0 else "OFFLINE"
    ping = parse_ping_time(result.stdout) if raw_status == "ONLINE" else "--"
    return {**device, "raw_status": raw_status, "ping": ping}


def format_duration(start_iso, end_dt):
    start = datetime.fromisoformat(start_iso)
    return str(end_dt - start).split(".")[0]


def update_status(raw_result, checked_at):
    device_id = raw_result["id"]
    recovered_report = None
    is_muted = raw_result.get("muted") in (True, 1, "1")
    state = MONITOR_STATE.setdefault(
        device_id,
        {
            "status": "UNKNOWN",
            "failure_streak": 0,
            "success_streak": 0,
            "first_failure": None,
            "offline_start": None,
            "down_count": 0,
            "long_alert_sent": False
        },
    )

    raw_status = raw_result["raw_status"]
    previous_status = state["status"]

    company_match = database.find_company_by_id(raw_result["company_id"])
    if company_match:
        target_receiver = (
            company_match.get("receivers")
            or company_match.get("email")
            or email_alerts.EMAIL_CONFIG.get("receiver_email", "")
        )
    else:
        target_receiver = email_alerts.EMAIL_CONFIG.get("receiver_email", "")

    if raw_status == "ONLINE":
        state["success_streak"] += 1
        state["failure_streak"] = 0
        state["first_failure"] = None

        recovered_report = None
        if previous_status == "OFFLINE" and state["success_streak"] >= ONLINE_AFTER_SUCCESSES:
            offline_start = state["offline_start"]
            if offline_start:
                recovered_report = {
                    "id": make_id("rep"),
                    "company_id": raw_result["company_id"],
                    "device_id": raw_result["id"],
                    "user_id": raw_result["user_id"],
                    "date": datetime.fromisoformat(offline_start).strftime("%d-%m-%Y"),
                    "location": raw_result["location"],
                    "name": raw_result["name"],
                    "ip": raw_result["ip"],
                    "offline": datetime.fromisoformat(offline_start).strftime("%H:%M:%S"),
                    "online": checked_at.strftime("%H:%M:%S"),
                    "downtime": format_duration(offline_start, checked_at),
                    "created_at": now_iso(),
                }

                # Always send a recovery email when device comes back online (unless muted)
                outage_data = {
                    "name": raw_result["name"],
                    "ip": raw_result["ip"],
                    "location": raw_result["location"],
                    "offline_time": datetime.fromisoformat(offline_start),
                    "online_time": checked_at,
                    "downtime": recovered_report["downtime"],
                }
                if not is_muted:
                    try:
                        email_alerts.send_short_outage_report(outage_data, target_receiver)
                    except Exception as e:
                        print(f"[Email] Failed to send recovery report: {e}")

            state["status"] = "ONLINE"
            state["offline_start"] = None
            state["long_alert_sent"] = False
        elif previous_status == "UNKNOWN":
            state["status"] = "ONLINE"

        display_status = state["status"]
        down_for = "-"
    else:
        state["failure_streak"] += 1
        state["success_streak"] = 0
        state["first_failure"] = state["first_failure"] or checked_at.isoformat(timespec="seconds")

        if previous_status != "OFFLINE" and state["failure_streak"] >= OFFLINE_AFTER_FAILURES:
            state["status"] = "OFFLINE"
            state["down_count"] += 1
            state["offline_start"] = state["first_failure"]

        if state["status"] != "OFFLINE" and state["failure_streak"] > 0:
            display_status = f"CHECK {state['failure_streak']}/{OFFLINE_AFTER_FAILURES}"
        else:
            display_status = state["status"]

        if state["status"] == "OFFLINE" and not state.get("long_alert_sent", False):
            offline_dt = datetime.fromisoformat(state["offline_start"])
            seconds_down = (checked_at - offline_dt).total_seconds()
            
            if seconds_down >= email_alerts.LONG_OUTAGE_ALERT_SECONDS:
                offline_info = {
                    "time": offline_dt,
                    "location": raw_result["location"],
                    "name": raw_result["name"],
                    "recipient": target_receiver
                }
                if not is_muted:
                    try:
                        email_alerts.send_long_outage_alert(offline_info, raw_result["ip"], checked_at, target_receiver)
                        state["long_alert_sent"] = True
                    except Exception as e:
                        print(f"Failed to send long outage email: {e}")
                else:
                    state["long_alert_sent"] = True

        down_for = format_duration(state["offline_start"], checked_at) if state["offline_start"] else "-"

    database.insert_ping_log(
        device_id,
        raw_result["user_id"],
        checked_at.isoformat(timespec="seconds"),
        raw_status,
        raw_result["ping"],
        down_for,
    )

    return display_status, down_for, recovered_report


def get_live_status(company_id):
    devices = company_devices(company_id)
    if not devices:
        return []

    checked_at = datetime.now()
    max_workers = min(MAX_WORKERS, max(1, len(devices)))
    results = []
    reports_to_add = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ping_device, device): device for device in devices}
        for future in as_completed(futures):
            raw = future.result()
            with STATE_LOCK:
                status, down_for, report = update_status(raw, checked_at)
                state = MONITOR_STATE[raw["id"]]
            if report:
                reports_to_add.append(report)
            results.append(
                {
                    "id": raw["id"],
                    "company_id": raw["company_id"],
                    "location": raw["location"],
                    "name": raw["name"],
                    "ip": raw["ip"],
                    "status": status,
                    "confirmedStatus": state["status"],
                    "ping": raw["ping"],
                    "downCount": state["down_count"],
                    "downFor": down_for,
                    "muted": raw.get("muted", 0),
                }
            )

    if reports_to_add:
        database.insert_reports(reports_to_add)

    return sorted(results, key=lambda item: (item["status"] != "OFFLINE", item["location"], item["name"]))


def parse_csv_devices(csv_text, company_id, user_id):
    rows = list(csv.DictReader(csv_text.splitlines()))
    devices = []
    for row in rows:
        devices.append(
            clean_device(
                {
                    "location": row.get("Location") or row.get("location"),
                    "name": row.get("Name") or row.get("Device Name") or row.get("name"),
                    "ip": row.get("IP") or row.get("IP Address") or row.get("ip"),
                },
                company_id,
                user_id,
            )
        )
    return devices


def parse_timeframe(timeframe, start_str, end_str):
    now = datetime.now()
    if timeframe == "today":
        start_dt = datetime(now.year, now.month, now.day)
        end_dt = now
    elif timeframe == "week":
        start_dt = now - timedelta(days=7)
        end_dt = now
    elif timeframe == "month":
        start_dt = now - timedelta(days=30)
        end_dt = now
    elif timeframe == "6months":
        start_dt = now - timedelta(days=180)
        end_dt = now
    elif timeframe == "custom":
        try:
            start_dt = datetime.fromisoformat(start_str.split("T")[0])
            end_dt = datetime.fromisoformat(end_str.split("T")[0]).replace(hour=23, minute=59, second=59)
        except Exception:
            start_dt = now - timedelta(days=7)
            end_dt = now
    else:
        start_dt = now - timedelta(days=7)
        end_dt = now
    return start_dt, end_dt


def clean_sheet_title(name, location):
    title = f"{name} ({location})"
    title = re.sub(r"[\\/\?\*:\[\]]", "", title)
    if len(title) > 31:
        title = title[:28] + "..."
    return title


def aggregate_pings(rows, timeframe, start_dt, end_dt):
    """Aggregate ping rows into chart-friendly buckets.
    Returns labels, uptime %, overall average, and chart type hint."""

    if timeframe == "today":
        chart_type = "bar"
        buckets = ["12 AM - 6 AM", "6 AM - 12 PM", "12 PM - 6 PM", "6 PM - 12 AM"]
        bucket_data = {b: {"total": 0, "online": 0, "latencies": []} for b in buckets}
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                h = ts.hour
                if 0 <= h < 6:
                    key = "12 AM - 6 AM"
                elif 6 <= h < 12:
                    key = "6 AM - 12 PM"
                elif 12 <= h < 18:
                    key = "12 PM - 6 PM"
                else:
                    key = "6 PM - 12 AM"
                
                bucket_data[key]["total"] += 1
                if r["status"] == "ONLINE":
                    bucket_data[key]["online"] += 1
                lat = r["latency"]
                if lat and lat != "--":
                    nums = re.findall(r"[\d\.]+", lat)
                    if nums:
                        bucket_data[key]["latencies"].append(float(nums[0]))
            except Exception:
                continue
                
        labels = buckets
        uptime_data = []
        latency_data = []
        for b in buckets:
            d = bucket_data[b]
            uptime_pct = round((d["online"] / d["total"]) * 100) if d["total"] > 0 else None
            avg_lat = round(sum(d["latencies"]) / len(d["latencies"]), 1) if d["latencies"] else None
            uptime_data.append(uptime_pct)
            latency_data.append(avg_lat)

    elif timeframe == "week":
        chart_type = "bar"
        group_format = "%Y-%m-%d"
        display_format = "%b %d"  # e.g., Jul 03
        buckets = []
        curr = datetime(start_dt.year, start_dt.month, start_dt.day)
        while curr.date() <= end_dt.date():
            buckets.append(curr.strftime(group_format))
            curr += timedelta(days=1)
            
        bucket_data = {b: {"total": 0, "online": 0, "latencies": []} for b in buckets}
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                key = ts.strftime(group_format)
                if key in bucket_data:
                    bucket_data[key]["total"] += 1
                    if r["status"] == "ONLINE":
                        bucket_data[key]["online"] += 1
                    lat = r["latency"]
                    if lat and lat != "--":
                        nums = re.findall(r"[\d\.]+", lat)
                        if nums:
                            bucket_data[key]["latencies"].append(float(nums[0]))
            except Exception:
                continue
                
        labels = []
        uptime_data = []
        latency_data = []
        for b in buckets:
            try:
                dt = datetime.strptime(b, group_format)
                labels.append(dt.strftime(display_format))
            except Exception:
                labels.append(b)
            d = bucket_data[b]
            uptime_pct = round((d["online"] / d["total"]) * 100) if d["total"] > 0 else None
            avg_lat = round(sum(d["latencies"]) / len(d["latencies"]), 1) if d["latencies"] else None
            uptime_data.append(uptime_pct)
            latency_data.append(avg_lat)

    elif timeframe == "month":
        chart_type = "bar"
        buckets = ["Week 1", "Week 2", "Week 3", "Week 4", "Week 5"]
        bucket_data = {b: {"total": 0, "online": 0, "latencies": []} for b in buckets}
        
        start_date_only = start_dt.date()
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                delta_days = (ts.date() - start_date_only).days
                if delta_days < 7:
                    key = "Week 1"
                elif delta_days < 14:
                    key = "Week 2"
                elif delta_days < 21:
                    key = "Week 3"
                elif delta_days < 28:
                    key = "Week 4"
                else:
                    key = "Week 5"
                    
                bucket_data[key]["total"] += 1
                if r["status"] == "ONLINE":
                    bucket_data[key]["online"] += 1
                lat = r["latency"]
                if lat and lat != "--":
                    nums = re.findall(r"[\d\.]+", lat)
                    if nums:
                        bucket_data[key]["latencies"].append(float(nums[0]))
            except Exception:
                continue
                
        labels = buckets
        uptime_data = []
        latency_data = []
        for b in buckets:
            d = bucket_data[b]
            uptime_pct = round((d["online"] / d["total"]) * 100) if d["total"] > 0 else None
            avg_lat = round(sum(d["latencies"]) / len(d["latencies"]), 1) if d["latencies"] else None
            uptime_data.append(uptime_pct)
            latency_data.append(avg_lat)

    else:
        chart_type = "bar"
        group_format = "%Y-%m-%d"
        display_format = "%b %d"
        buckets = []
        curr = datetime(start_dt.year, start_dt.month, start_dt.day)
        while curr.date() <= end_dt.date():
            buckets.append(curr.strftime(group_format))
            curr += timedelta(days=1)
            
        bucket_data = {b: {"total": 0, "online": 0, "latencies": []} for b in buckets}
        for r in rows:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                key = ts.strftime(group_format)
                if key in bucket_data:
                    bucket_data[key]["total"] += 1
                    if r["status"] == "ONLINE":
                        bucket_data[key]["online"] += 1
                    lat = r["latency"]
                    if lat and lat != "--":
                        nums = re.findall(r"[\d\.]+", lat)
                        if nums:
                            bucket_data[key]["latencies"].append(float(nums[0]))
            except Exception:
                continue
                
        labels = []
        uptime_data = []
        latency_data = []
        for b in buckets:
            try:
                dt = datetime.strptime(b, group_format)
                labels.append(dt.strftime(display_format))
            except Exception:
                labels.append(b)
            d = bucket_data[b]
            uptime_pct = round((d["online"] / d["total"]) * 100) if d["total"] > 0 else None
            avg_lat = round(sum(d["latencies"]) / len(d["latencies"]), 1) if d["latencies"] else None
            uptime_data.append(uptime_pct)
            latency_data.append(avg_lat)

    # --- Overall average across all pings for true running live average ---
    total_pings = len(rows)
    online_pings = sum(1 for r in rows if r["status"] == "ONLINE")
    overall_avg = round((online_pings / total_pings) * 100) if total_pings > 0 else 100

    return {
        "labels": labels,
        "uptime": uptime_data,
        "latency": latency_data,
        "chartType": chart_type,
        "overallAvg": overall_avg,
    }


class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/me":
            user = get_session_user(self)
            if not user:
                self.send_json({"authenticated": False})
                return
            self.send_json({"authenticated": True, "user": public_user(user)})
            return

        if path == "/api/companies":
            user = self.require_user()
            if not user:
                return
            self.send_json({"companies": user_companies(user["id"])})
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/devices", path)
        if match:
            user = self.require_user()
            if not user:
                return
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
            self.send_json({"devices": company_devices(company_id)})
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/reports", path)
        if match:
            user = self.require_user()
            if not user:
                return
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
            self.send_json({"reports": company_reports(company_id)})
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/status", path)
        if match:
            user = self.require_user()
            if not user:
                return
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
            self.send_json({"devices": get_live_status(company_id)})
            return

        # NEW HISTORICAL ANALYTICS ENDPOINT
        match = re.fullmatch(r"/api/devices/([^/]+)/analytics", path)
        if match:
            user = self.require_user()
            if not user:
                return
            device_id = unquote(match.group(1))
            device = database.get_device_for_user(device_id, user["id"])
            if not device:
                self.send_json({"error": "Device not found"}, 404)
                return
                
            timeframe = query.get("timeframe", ["week"])[0]
            start_str = query.get("start", [""])[0]
            end_str = query.get("end", [""])[0]
            
            start_dt, end_dt = parse_timeframe(timeframe, start_str, end_str)
            start_iso = start_dt.isoformat(timespec="seconds")
            end_iso = end_dt.isoformat(timespec="seconds")
            
            rows_dict = database.get_device_ping_logs(device_id, user["id"], start_iso, end_iso)
            total_checks = len(rows_dict)
            online_checks = sum(1 for r in rows_dict if r["status"] == "ONLINE")
            avg_health = round((online_checks / total_checks) * 100) if total_checks > 0 else 100
            
            aggregation = aggregate_pings(rows_dict, timeframe, start_dt, end_dt)
            
            today_hourly = None
            if timeframe == "today":
                hourly_labels = []
                for h in range(24):
                    if h == 0:
                        hourly_labels.append("12 AM")
                    elif h < 12:
                        hourly_labels.append(f"{h} AM")
                    elif h == 12:
                        hourly_labels.append("12 PM")
                    else:
                        hourly_labels.append(f"{h-12} PM")
                
                bucket_counts = {lbl: {"total": 0, "online": 0} for lbl in hourly_labels}
                for r in rows_dict:
                    try:
                        ts = datetime.fromisoformat(r["timestamp"])
                        h = ts.hour
                        lbl = hourly_labels[h]
                        bucket_counts[lbl]["total"] += 1
                        if r["status"] == "ONLINE":
                            bucket_counts[lbl]["online"] += 1
                    except Exception:
                        continue
                
                today_hourly = {
                    "labels": hourly_labels,
                    "uptime": [
                        round((bucket_counts[lbl]["online"] / bucket_counts[lbl]["total"]) * 100)
                        if bucket_counts[lbl]["total"] > 0 else None
                        for lbl in hourly_labels
                    ]
                }
            
            self.send_json({
                "avgHealth": avg_health,
                "overallAvg": aggregation["overallAvg"],
                "chartType": aggregation["chartType"],
                "labels": aggregation["labels"],
                "uptime": aggregation["uptime"],
                "latency": aggregation["latency"],
                "todayHourly": today_hourly
            })
            return

        # NEW EXCEL REPORT EXPORT ENDPOINT
        match = re.fullmatch(r"/api/companies/([^/]+)/analytics/export", path)
        if match:
            user = self.require_user()
            if not user:
                return
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
                
            timeframe = query.get("timeframe", ["week"])[0]
            start_str = query.get("start", [""])[0]
            end_str = query.get("end", [""])[0]
            device_id = query.get("device_id", [""])[0]
            
            start_dt, end_dt = parse_timeframe(timeframe, start_str, end_str)
            start_iso = start_dt.isoformat(timespec="seconds")
            end_iso = end_dt.isoformat(timespec="seconds")
            
            devices = company_devices(company_id)
            if device_id:
                devices = [d for d in devices if d["id"] == device_id]
            
            wb = Workbook()
            default_sheet = wb.active
            wb.remove(default_sheet)

            HDR_FONT  = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
            HDR_FILL  = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
            HDR_ALIGN = Alignment(horizontal="center", vertical="center")
            SUB_FONT  = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
            SUB_FILL  = PatternFill(start_color="2E75B6", end_color="2E75B6", fill_type="solid")
            AVG_FILL  = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
            AVG_FONT  = Font(name="Segoe UI", size=10, bold=True)

            def style_row(ws, row_num, font=None, fill=None, align=None, n_cols=4):
                for col in range(1, n_cols + 1):
                    c = ws.cell(row=row_num, column=col)
                    if font:  c.font  = font
                    if fill:  c.fill  = fill
                    if align: c.alignment = align

            def auto_width(ws, n_cols):
                for col_idx in range(1, n_cols + 1):
                    col_letter = get_column_letter(col_idx)
                    max_len = max(
                        (len(str(ws.cell(row=r, column=col_idx).value or '')) for r in range(1, ws.max_row + 1)),
                        default=8
                    )
                    ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

            def ping_health_for_period(pings_rows, from_dt, to_dt):
                """Return (total, online, avg_latency_ms) for a window."""
                total = online = 0
                lats = []
                for r in pings_rows:
                    try:
                        ts = datetime.fromisoformat(r["timestamp"])
                        if from_dt <= ts <= to_dt:
                            total += 1
                            if r["status"] == "ONLINE":
                                online += 1
                            lat = r["latency"]
                            if lat and lat != "--":
                                nums = re.findall(r"[\d\.]+", lat)
                                if nums: lats.append(float(nums[0]))
                    except Exception:
                        continue
                health = round((online / total) * 100, 1) if total else None
                avg_lat = round(sum(lats) / len(lats), 1) if lats else None
                return total, online, health, avg_lat

            for dev in devices:
                sheet_title = clean_sheet_title(dev["name"], dev["location"])
                ws = wb.create_sheet(title=sheet_title)

                # --- Title rows ---
                ws.append([f"Health Report: {dev['name']} [{dev['location']}] ({dev['ip']})"])
                ws.append([f"Period: {start_dt.strftime('%d-%m-%Y')} to {end_dt.strftime('%d-%m-%Y')} | Timeframe: {timeframe.upper()}"])
                ws.append([])

                # --- Fetch all raw pings for this device in the range ---
                raw_rows = database.get_device_ping_logs(dev["id"], user["id"], start_iso, end_iso)

                if timeframe == "today":
                    # --- TODAY: 4 periods health breakdown ---
                    n_cols = 4
                    ws.append(["Period", "Health %", "Avg Latency (ms)", "Checks"])
                    style_row(ws, ws.max_row, HDR_FONT, HDR_FILL, HDR_ALIGN, n_cols)
                    
                    periods = [
                        ("12 AM - 6 AM", 0, 5),
                        ("6 AM - 12 PM", 6, 11),
                        ("12 PM - 6 PM", 12, 17),
                        ("6 PM - 12 AM", 18, 23)
                    ]
                    for label, start_h, end_h in periods:
                        p_start = datetime(start_dt.year, start_dt.month, start_dt.day, start_h, 0, 0)
                        p_end   = datetime(start_dt.year, start_dt.month, start_dt.day, end_h, 59, 59)
                        total, online, health, avg_lat = ping_health_for_period(raw_rows, p_start, p_end)
                        ws.append([
                            label,
                            f"{round(health)}%" if health is not None else "No data",
                            f"{avg_lat} ms" if avg_lat is not None else "-",
                            total
                        ])
                    
                    # Summary row: calculate overall average from all pings (running live average)
                    total_pings = len(raw_rows)
                    online_pings = sum(1 for r in raw_rows if r["status"] == "ONLINE")
                    day_health = round((online_pings / total_pings) * 100) if total_pings > 0 else None
                    lats = []
                    for r in raw_rows:
                        lat = r["latency"]
                        if lat and lat != "--":
                            nums = re.findall(r"[\d\.]+", lat)
                            if nums: lats.append(float(nums[0]))
                    day_lat = round(sum(lats) / len(lats), 1) if lats else None

                    ws.append([])
                    ws.append(["TODAY AVERAGE", f"{day_health}%" if day_health is not None else "No data", f"{day_lat} ms" if day_lat is not None else "-", total_pings])
                    style_row(ws, ws.max_row, AVG_FONT, AVG_FILL, None, n_cols)
                    auto_width(ws, n_cols)

                elif timeframe == "week":
                    # --- WEEK: Daily summary + overall average ---
                    n_cols = 4
                    ws.append(["Day", "Health %", "Avg Latency (ms)", "Checks"])
                    style_row(ws, ws.max_row, HDR_FONT, HDR_FILL, HDR_ALIGN, n_cols)
                    curr = datetime(start_dt.year, start_dt.month, start_dt.day)
                    
                    while curr.date() <= end_dt.date():
                        d_end = curr + timedelta(hours=23, minutes=59, seconds=59)
                        total, online, health, avg_lat = ping_health_for_period(raw_rows, curr, d_end)
                        ws.append([
                            curr.strftime("%b %d (%a)"),
                            f"{round(health)}%" if health is not None else "No data",
                            f"{avg_lat} ms" if avg_lat is not None else "-",
                            total
                        ])
                        curr += timedelta(days=1)
                    
                    # Summary row: running live average from raw pings
                    total_pings = len(raw_rows)
                    online_pings = sum(1 for r in raw_rows if r["status"] == "ONLINE")
                    week_avg = round((online_pings / total_pings) * 100) if total_pings > 0 else None
                    
                    ws.append([])
                    ws.append(["WEEK AVERAGE", f"{week_avg}%" if week_avg is not None else "No data", "", total_pings])
                    style_row(ws, ws.max_row, AVG_FONT, AVG_FILL, None, n_cols)
                    auto_width(ws, n_cols)

                elif timeframe == "month":
                    # --- MONTH: Weekly summaries only ---
                    n_cols = 4
                    ws.append(["Week", "Period Range", "Health %", "Checks"])
                    style_row(ws, ws.max_row, HDR_FONT, HDR_FILL, HDR_ALIGN, n_cols)
                    
                    week_periods = [
                        ("Week 1", start_dt, start_dt + timedelta(days=6)),
                        ("Week 2", start_dt + timedelta(days=7), start_dt + timedelta(days=13)),
                        ("Week 3", start_dt + timedelta(days=14), start_dt + timedelta(days=20)),
                        ("Week 4", start_dt + timedelta(days=21), start_dt + timedelta(days=27)),
                        ("Week 5", start_dt + timedelta(days=28), end_dt)
                    ]
                    
                    for label, w_start, w_end in week_periods:
                        w_start_dt = datetime(w_start.year, w_start.month, w_start.day, 0, 0, 0)
                        w_end_dt = datetime(w_end.year, w_end.month, w_end.day, 23, 59, 59)
                        if w_start_dt.date() > end_dt.date():
                            continue
                        if w_end_dt.date() > end_dt.date():
                            w_end_dt = end_dt
                            
                        total, online, health, avg_lat = ping_health_for_period(raw_rows, w_start_dt, w_end_dt)
                        ws.append([
                            label,
                            f"{w_start_dt.strftime('%b %d')} – {w_end_dt.strftime('%b %d')}",
                            f"{round(health)}%" if health is not None else "No data",
                            total
                        ])
                    
                    # Summary row: running live average from raw pings
                    total_pings = len(raw_rows)
                    online_pings = sum(1 for r in raw_rows if r["status"] == "ONLINE")
                    month_avg = round((online_pings / total_pings) * 100) if total_pings > 0 else None
                    
                    ws.append([])
                    ws.append(["MONTH AVERAGE", "", f"{month_avg}%" if month_avg is not None else "No data", total_pings])
                    style_row(ws, ws.max_row, AVG_FONT, AVG_FILL, None, n_cols)
                    auto_width(ws, n_cols)

                else:
                    # --- CUSTOM/OTHER: Daily breakdown ---
                    n_cols = 4
                    ws.append(["Date", "Health %", "Avg Latency (ms)", "Checks"])
                    style_row(ws, ws.max_row, HDR_FONT, HDR_FILL, HDR_ALIGN, n_cols)
                    
                    curr = datetime(start_dt.year, start_dt.month, start_dt.day)
                    while curr.date() <= end_dt.date():
                        d_end = curr + timedelta(hours=23, minutes=59, seconds=59)
                        total, online, health, avg_lat = ping_health_for_period(raw_rows, curr, d_end)
                        ws.append([
                            curr.strftime("%b %d (%a)"),
                            f"{round(health)}%" if health is not None else "No data",
                            f"{avg_lat} ms" if avg_lat is not None else "-",
                            total
                        ])
                        curr += timedelta(days=1)
                        
                    # Summary row
                    total_pings = len(raw_rows)
                    online_pings = sum(1 for r in raw_rows if r["status"] == "ONLINE")
                    overall_avg = round((online_pings / total_pings) * 100) if total_pings > 0 else None
                    
                    ws.append([])
                    ws.append(["OVERALL AVERAGE", f"{overall_avg}%" if overall_avg is not None else "No data", "", total_pings])
                    style_row(ws, ws.max_row, AVG_FONT, AVG_FILL, None, n_cols)
                    auto_width(ws, n_cols)
            
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            excel_bytes = output.read()
            
            def sanitize_filename_part(text):
                return re.sub(r"[^a-zA-Z0-9_\-]", "_", str(text).strip())

            dev_name = sanitize_filename_part(devices[0]["name"]) if devices else "All_Devices"
            dev_loc = sanitize_filename_part(devices[0]["location"]) if devices else "Global"
            
            if timeframe == "today":
                date_str = start_dt.strftime("%Y-%m-%d")
                filename = f"Report_{dev_name}_{dev_loc}_{date_str}.xlsx"
            elif timeframe == "week":
                filename = f"Report_{dev_name}_{dev_loc}_Weekly_Report_{start_dt.strftime('%Y%m%d')}_to_{end_dt.strftime('%Y%m%d')}.xlsx"
            elif timeframe == "month":
                month_name = start_dt.strftime("%B_%Y")
                filename = f"Report_{month_name}_{dev_name}_{dev_loc}.xlsx"
            else: # custom
                filename = f"Report_{dev_name}_{dev_loc}_Custom_{start_dt.strftime('%Y%m%d')}_to_{end_dt.strftime('%Y%m%d')}.xlsx"

            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{filename}"'
            )
            self.send_header("Content-Length", str(len(excel_bytes)))
            self.end_headers()
            self.wfile.write(excel_bytes)
            return

        self.send_static(path)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/signup":
            data = self.read_json()
            name = str(data.get("name", "")).strip() or "User"
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            company_name = str(data.get("company", "")).strip() or "My Company"

            if not email or not password:
                self.send_json({"error": "Email and password are required"}, 400)
                return

            if get_user_by_email(email):
                self.send_json({"error": "Account already exists"}, 400)
                return

            user = database.create_user_record(name, email, password, company_name)
            database.bootstrap_user_data(user["id"], company_name)
            companies = user_companies(user["id"])
            company = companies[0] if companies else None

            self.create_session(user)
            self.send_json({"user": public_user(user), "company": company}, 201)
            return

        if path == "/api/login":
            data = self.read_json()
            email = str(data.get("email", "")).strip().lower()
            password = str(data.get("password", ""))
            user = get_user_by_email(email)
            
            if not user or not verify_password(password, user["password_hash"]):
                self.send_json({"error": "Invalid email or password"}, 401)
                return
                
            self.create_session(user)
            self.send_json({"user": public_user(user)})
            return

        if path == "/api/logout":
            self.clear_session()
            self.send_json({"ok": True})
            return

        user = self.require_user()
        if not user:
            return

        if path == "/api/companies":
            data = self.read_json()
            target_email = str(data.get("email", "")).strip()
            company = database.create_company(
                user["id"],
                str(data.get("name", "")).strip() or "New Company",
                target_email,
                target_email,
                30,
            )
                
            self.send_json({"company": company}, 201)
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/devices", path)
        if match:
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
                
            device = clean_device(self.read_json(), company_id, user["id"])
            database.create_device(device)
                
            with STATE_LOCK:
                MONITOR_STATE.pop(device["id"], None)
                
            self.send_json({"device": device}, 201)
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/devices/import", path)
        if match:
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
                
            data = self.read_json()
            devices = parse_csv_devices(str(data.get("csv", "")), company_id, user["id"])
            database.create_devices(devices)
                
            self.send_json({"imported": len(devices)})
            return

        self.send_json({"error": "Not found"}, 404)

    def do_PUT(self):
        user = self.require_user()
        if not user:
            return

        path = urlparse(self.path).path

        match = re.fullmatch(r"/api/companies/([^/]+)", path)
        if match:
            company_id = unquote(match.group(1))
            company = find_company(user["id"], company_id)
            if not company:
                self.send_json({"error": "Company not found"}, 404)
                return
                
            data = self.read_json()
            company["name"] = str(data.get("name", company["name"])).strip() or company["name"]
            
            update_email = str(data.get("receivers", company.get("receivers", ""))).strip()
            if update_email:
                company["email"] = update_email
                company["receivers"] = update_email
                
            company["alert_after_seconds"] = int(data.get("alert_after_seconds", company.get("alert_after_seconds", 30)))
            
            database.update_company(
                company_id,
                company["name"],
                company["email"],
                company["receivers"],
                company["alert_after_seconds"],
            )
                
            self.send_json({"company": company})
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/devices/([^/]+)", path)
        if match:
            company_id = unquote(match.group(1))
            device_id = unquote(match.group(2))
            
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
                
            device = database.get_device_in_company(device_id, company_id, user["id"])
            if not device:
                self.send_json({"error": "Device not found"}, 404)
                return
                
            updated = clean_device({**device, **self.read_json(), "id": device_id}, company_id, user["id"])
            database.update_device(
                device_id,
                company_id,
                user["id"],
                updated["location"],
                updated["name"],
                updated["ip"],
                updated["muted"],
            )
                
            with STATE_LOCK:
                MONITOR_STATE.pop(device_id, None)
                
            self.send_json({"device": updated})
            return

        self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        user = self.require_user()
        if not user:
            return

        path = urlparse(self.path).path

        match = re.fullmatch(r"/api/companies/([^/]+)$", path)
        if match:
            company_id = unquote(match.group(1))
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
            if len(user_companies(user["id"])) <= 1:
                self.send_json({"error": "At least one company is required"}, 400)
                return
                
            database.delete_company(company_id)
                
            self.send_json({"ok": True})
            return

        match = re.fullmatch(r"/api/companies/([^/]+)/devices/([^/]+)", path)
        if match:
            company_id = unquote(match.group(1))
            device_id = unquote(match.group(2))
            
            if not find_company(user["id"], company_id):
                self.send_json({"error": "Company not found"}, 404)
                return
                
            if not database.get_device_in_company(device_id, company_id, user["id"]):
                self.send_json({"error": "Device not found"}, 404)
                return

            database.delete_device(device_id, company_id, user["id"])
            
            with STATE_LOCK:
                MONITOR_STATE.pop(device_id, None)
                
            self.send_json({"ok": True})
            return

        self.send_json({"error": "Not found"}, 404)

    def require_user(self):
        user = get_session_user(self)
        if not user:
            self.send_json({"error": "Unauthorized access"}, 401)
            return None
        return user

    def create_session(self, user):
        token = database.create_session(user["id"])
        self.extra_cookie = f"netwatch_session={token}; HttpOnly; Path=/; SameSite=Lax"

    def clear_session(self):
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie(cookie_header)
        token = cookie.get("netwatch_session")
        if token:
            database.delete_session(token.value)
        self.extra_cookie = "netwatch_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        return json.loads(raw_body or "{}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if hasattr(self, "extra_cookie"):
            self.send_header("Set-Cookie", self.extra_cookie)
            del self.extra_cookie
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, request_path):
        if request_path == "/":
            request_path = "/index.html"
        
        clean_path = request_path.lstrip("/").replace("\\", "/")
        file_path = (WEB_DIR / clean_path).resolve()
        
        if not str(file_path).startswith(str(WEB_DIR.resolve())) or not file_path.exists():
            self.send_error(404)
            return
            
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def main():
    database.run_migrations()
    
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"NetWatch is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()