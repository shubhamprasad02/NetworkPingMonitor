import csv
import os
import platform
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from email_alerts import (
    LONG_OUTAGE_ALERT_SECONDS,
    SHORT_OUTAGE_MONITOR_SECONDS,
    get_long_outage_seconds,
    send_fluctuation_report,
    send_long_outage_alert,
    send_short_outage_report,
)


APP_VERSION = "2.0"

BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.csv"
REPORT_FILE = BASE_DIR / "outage_report.csv"

REFRESH_SECONDS = 2
PING_TIMEOUT_SECONDS = 1
MAX_WORKERS = 20

OFFLINE_AFTER_FAILURES = 3
ONLINE_AFTER_SUCCESSES = 1

REPORT_HEADER = [
    "Date",
    "Location",
    "Device Name",
    "IP Address",
    "Offline Time",
    "Online Time",
    "Downtime",
]


class TenantContext:
    def __init__(self, tenant_id, report_path, alert_config=None):
        self.tenant_id = tenant_id
        self.report_path = Path(report_path)
        self.alert_config = alert_config or {}
        self.device_status = {}
        self.failure_streak = {}
        self.success_streak = {}
        self.first_failure_time = {}
        self.offline_start = {}
        self.down_count = {}
        self.long_outage_alert_sent = {}
        self.short_outage_windows = {}


tenant_contexts = {}


def get_tenant_context(tenant_id, report_path, alert_config=None):
    context = tenant_contexts.get(tenant_id)

    if context is None or context.report_path != Path(report_path):
        context = TenantContext(tenant_id, report_path, alert_config)
        tenant_contexts[tenant_id] = context
    elif alert_config is not None:
        context.alert_config = alert_config

    return context


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def ensure_report_file(report_path=REPORT_FILE):
    report_path = Path(report_path)
    valid_rows = []

    if report_path.exists():
        with report_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.reader(file)
            rows = list(reader)

        for row in rows:
            if row == REPORT_HEADER:
                continue
            if len(row) == len(REPORT_HEADER):
                valid_rows.append(row)

    with report_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(REPORT_HEADER)
        writer.writerows(valid_rows)


def load_devices(devices_file=DEVICES_FILE):
    devices_file = Path(devices_file)

    if not devices_file.exists():
        raise FileNotFoundError(f"Missing devices file: {devices_file}")

    devices = []
    seen_ips = set()

    with devices_file.open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        required_columns = {"Location", "Name", "IP"}

        if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
            raise ValueError("devices.csv must have these columns: Location, Name, IP")

        for line_number, row in enumerate(reader, start=2):
            location = row.get("Location", "").strip()
            name = row.get("Name", "").strip()
            ip = row.get("IP", "").strip()

            if not ip:
                continue

            if ip in seen_ips:
                continue

            seen_ips.add(ip)
            devices.append(
                {
                    "location": location or "-",
                    "name": name or f"Device {line_number}",
                    "ip": ip,
                }
            )

    if not devices:
        raise ValueError("devices.csv does not contain any devices")

    return devices


def build_ping_command(ip):
    if platform.system().lower() == "windows":
        return ["ping", "-n", "1", "-w", str(PING_TIMEOUT_SECONDS * 1000), ip]

    return ["ping", "-c", "1", "-W", str(PING_TIMEOUT_SECONDS), ip]


def ping_device(device):
    try:
        result = subprocess.run(
            build_ping_command(device["ip"]),
            capture_output=True,
            text=True,
            timeout=PING_TIMEOUT_SECONDS + 1,
        )
    except subprocess.TimeoutExpired:
        return {**device, "raw_status": "OFFLINE", "ping": "--"}
    except OSError:
        return {**device, "raw_status": "ERROR", "ping": "--"}

    raw_status = "ONLINE" if result.returncode == 0 else "OFFLINE"
    ping_ms = parse_ping_time(result.stdout) if raw_status == "ONLINE" else "--"

    return {**device, "raw_status": raw_status, "ping": ping_ms}


def parse_ping_time(output):
    match = re.search(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", output, re.IGNORECASE)

    if not match:
        return "--"

    if "time<" in match.group(0).lower():
        return "<1 ms"

    return f"{match.group(1)} ms"


def ping_all(devices):
    max_workers = min(MAX_WORKERS, max(1, len(devices)))
    results_by_ip = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(ping_device, device): device for device in devices}

        for future in as_completed(futures):
            result = future.result()
            results_by_ip[result["ip"]] = result

    return [results_by_ip[device["ip"]] for device in devices]


def format_duration(start_time, end_time):
    return str(end_time - start_time).split(".")[0]


def write_outage_row(context, offline_info, ip, online_time):
    offline_time = offline_info["time"]
    downtime = format_duration(offline_time, online_time)

    with context.report_path.open("a", newline="", encoding="utf-8") as report:
        writer = csv.writer(report)
        writer.writerow(
            [
                offline_time.strftime("%d-%m-%Y"),
                offline_info["location"],
                offline_info["name"],
                ip,
                offline_time.strftime("%H:%M:%S"),
                online_time.strftime("%H:%M:%S"),
                downtime,
            ]
        )

    return {
        "location": offline_info["location"],
        "name": offline_info["name"],
        "ip": ip,
        "offline_time": offline_time,
        "online_time": online_time,
        "downtime": downtime,
    }


def remember_offline(context, result, offline_time):
    context.offline_start[result["ip"]] = {
        "time": offline_time,
        "location": result["location"],
        "name": result["name"],
    }
    context.long_outage_alert_sent[result["ip"]] = False


def maybe_send_long_outage_alert(context, ip, checked_at):
    offline_info = context.offline_start.get(ip)

    if not offline_info or context.long_outage_alert_sent.get(ip):
        return

    offline_seconds = (checked_at - offline_info["time"]).total_seconds()
    threshold = get_long_outage_seconds(context.alert_config)

    if offline_seconds > threshold:
        send_long_outage_alert(offline_info, ip, checked_at, context.alert_config)
        context.long_outage_alert_sent[ip] = True
        context.short_outage_windows.pop(ip, None)


def remember_short_outage(context, outage):
    ip = outage["ip"]
    window = context.short_outage_windows.get(ip)

    if not window:
        context.short_outage_windows[ip] = {
            "device": {
                "location": outage["location"],
                "name": outage["name"],
                "ip": ip,
            },
            "report_after": outage["online_time"].timestamp() + SHORT_OUTAGE_MONITOR_SECONDS,
            "outages": [outage],
        }
        return

    window["outages"].append(outage)
    window["device"].update(
        {
            "location": outage["location"],
            "name": outage["name"],
            "ip": ip,
        }
    )


def process_due_short_outage_reports(context, checked_at):
    due_ips = []

    for ip, window in context.short_outage_windows.items():
        if checked_at.timestamp() >= window["report_after"] and ip not in context.offline_start:
            due_ips.append(ip)

    for ip in due_ips:
        window = context.short_outage_windows.pop(ip)
        outages = window["outages"]

        if len(outages) == 1:
            if context.alert_config.get("send_short_outage_reports", True):
                send_short_outage_report(outages[0], context.alert_config)
        else:
            send_fluctuation_report(window["device"], outages, context.alert_config)


def update_device_state(context, result, checked_at):
    ip = result["ip"]
    raw_status = result["raw_status"]
    previous_status = context.device_status.get(ip, "UNKNOWN")

    context.down_count.setdefault(ip, 0)
    context.failure_streak.setdefault(ip, 0)
    context.success_streak.setdefault(ip, 0)

    if raw_status == "ONLINE":
        context.success_streak[ip] += 1
        context.failure_streak[ip] = 0
        context.first_failure_time.pop(ip, None)

        if previous_status == "OFFLINE" and context.success_streak[ip] >= ONLINE_AFTER_SUCCESSES:
            offline_info = context.offline_start.pop(ip, None)
            if offline_info:
                outage = write_outage_row(context, offline_info, ip, checked_at)
                outage_was_long = context.long_outage_alert_sent.pop(ip, False)
                outage_seconds = (checked_at - offline_info["time"]).total_seconds()
                threshold = get_long_outage_seconds(context.alert_config)
                if outage_was_long:
                    # Device recovered from a long outage — send immediate online notification
                    send_short_outage_report(outage, context.alert_config)
                elif (
                    outage_seconds < threshold
                    and context.alert_config.get("send_short_outage_reports", True)
                ):
                    remember_short_outage(context, outage)
            context.device_status[ip] = "ONLINE"
        elif previous_status == "UNKNOWN":
            context.device_status[ip] = "ONLINE"

    else:
        context.failure_streak[ip] += 1
        context.success_streak[ip] = 0
        context.first_failure_time.setdefault(ip, checked_at)

        if previous_status != "OFFLINE" and context.failure_streak[ip] >= OFFLINE_AFTER_FAILURES:
            context.device_status[ip] = "OFFLINE"
            context.down_count[ip] += 1
            remember_offline(context, result, context.first_failure_time[ip])
        elif previous_status == "OFFLINE":
            maybe_send_long_outage_alert(context, ip, checked_at)

    return build_dashboard_row(context, result, checked_at)


def build_dashboard_row(context, result, checked_at):
    ip = result["ip"]
    confirmed_status = context.device_status.get(ip, "UNKNOWN")
    missed = context.failure_streak.get(ip, 0)

    if confirmed_status != "OFFLINE" and missed > 0:
        display_status = f"CHECK {missed}/{OFFLINE_AFTER_FAILURES}"
    else:
        display_status = confirmed_status

    outage = context.offline_start.get(ip)
    down_for = format_duration(outage["time"], checked_at) if outage else "-"

    return {
        **result,
        "status": display_status,
        "confirmed_status": confirmed_status,
        "down_count": context.down_count.get(ip, 0),
        "down_for": down_for,
    }


def status_rank(result):
    if result["confirmed_status"] == "OFFLINE":
        return 0
    if result["status"].startswith("CHECK"):
        return 1
    if result["confirmed_status"] == "UNKNOWN":
        return 2
    return 3


def print_dashboard(results, checked_at):
    clear_screen()

    online_count = sum(1 for result in results if result["confirmed_status"] == "ONLINE")
    offline_count = sum(1 for result in results if result["confirmed_status"] == "OFFLINE")
    checking_count = len(results) - online_count - offline_count
    sorted_results = sorted(results, key=lambda item: (status_rank(item), item["location"], item["name"]))

    print(f"Ping Monitor v{APP_VERSION}")
    print(f"Updated: {checked_at.strftime('%d-%m-%Y %H:%M:%S')}")
    print(
        f"Devices: {len(results)} | "
        f"Online: {online_count} | "
        f"Offline: {offline_count} | "
        f"Checking: {checking_count}"
    )
    print(f"Outage rule: offline after {OFFLINE_AFTER_FAILURES} missed pings")
    print(f"Report: {REPORT_FILE}")
    print()
    print(
        f"{'Location':<14}"
        f"{'Device Name':<24}"
        f"{'IP Address':<18}"
        f"{'Status':<12}"
        f"{'Ping':<10}"
        f"{'Down Count':<12}"
        f"{'Down For'}"
    )
    print("-" * 104)

    for result in sorted_results:
        print(
            f"{result['location'][:13]:<14}"
            f"{result['name'][:23]:<24}"
            f"{result['ip']:<18}"
            f"{result['status']:<12}"
            f"{result['ping']:<10}"
            f"{result['down_count']:<12}"
            f"{result['down_for']}"
        )


def get_status_rows(tenant_id, devices, report_path, alert_config=None):
    if not devices:
        return []

    context = get_tenant_context(tenant_id, report_path, alert_config)
    checked_at = datetime.now()
    raw_results = ping_all(devices)
    rows = [update_device_state(context, result, checked_at) for result in raw_results]
    process_due_short_outage_reports(context, checked_at)

    return [
        {
            "id": index,
            "location": row["location"],
            "name": row["name"],
            "ip": row["ip"],
            "status": row["status"],
            "confirmedStatus": row["confirmed_status"],
            "ping": row["ping"],
            "downCount": row["down_count"],
            "downFor": row["down_for"],
        }
        for index, row in enumerate(rows)
    ]


def main():
    ensure_report_file()

    while True:
        checked_at = datetime.now()

        try:
            devices = load_devices()
            context = get_tenant_context("cli", REPORT_FILE)
            raw_results = ping_all(devices)
            dashboard_results = [update_device_state(context, result, checked_at) for result in raw_results]
            process_due_short_outage_reports(context, checked_at)
        except Exception as error:
            clear_screen()
            print(f"Ping Monitor v{APP_VERSION}")
            print(f"Error: {error}")
            time.sleep(REFRESH_SECONDS)
            continue

        print_dashboard(dashboard_results, checked_at)
        time.sleep(REFRESH_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped ping monitor.")
