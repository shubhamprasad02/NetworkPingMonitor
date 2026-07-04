import csv
import ipaddress

import auth
import database

BASE_DIR = database.BASE_DIR
TENANTS_DIR = database.TENANTS_DIR

DEVICE_HEADER = ["Location", "Name", "IP"]
REPORT_HEADER = [
    "Date",
    "Location",
    "Device Name",
    "IP Address",
    "Offline Time",
    "Online Time",
    "Downtime",
]

DEFAULT_SETTINGS = database.DEFAULT_SETTINGS
DEFAULT_ALERTS = database.DEFAULT_ALERTS


def tenant_dir(user_id):
    return TENANTS_DIR / user_id


def devices_file(user_id):
    return tenant_dir(user_id) / "devices.csv"


def report_file(user_id):
    return tenant_dir(user_id) / "outage_report.csv"


def settings_file(user_id):
    return tenant_dir(user_id) / "settings.json"


def alerts_file(user_id):
    return tenant_dir(user_id) / "alerts.json"


def ensure_tenant_files(user_id):
    database.bootstrap_user_data(user_id)


def has_only_csv_header(path):
    if not path.exists():
        return True

    with path.open("r", newline="", encoding="utf-8-sig") as file:
        rows = [row for row in csv.reader(file) if any(cell.strip() for cell in row)]

    return len(rows) <= 1


def migrate_legacy_data(user_id):
    ensure_tenant_files(user_id)


def read_devices(user_id):
    ensure_tenant_files(user_id)
    return database.list_user_devices_ordered(user_id)


def write_devices(user_id, devices):
    ensure_tenant_files(user_id)
    database.replace_user_devices(user_id, devices)


def read_reports(user_id):
    ensure_tenant_files(user_id)
    return database.user_reports(user_id)


def read_settings(user_id):
    ensure_tenant_files(user_id)
    return database.get_user_settings(user_id)


def save_settings(user_id, settings):
    ensure_tenant_files(user_id)
    database.save_user_settings(
        user_id,
        settings.get("timezone", DEFAULT_SETTINGS["timezone"]),
        settings.get("refresh_interval", DEFAULT_SETTINGS["refresh_interval"]),
    )


def read_alerts(user_id):
    ensure_tenant_files(user_id)
    return database.get_user_alerts(user_id)


def save_alerts(user_id, alerts):
    ensure_tenant_files(user_id)
    database.save_user_alerts(user_id, alerts)


def validate_ip(ip):
    value = ip.strip()

    if not value:
        raise ValueError("IP address is required")

    try:
        ipaddress.ip_address(value)
    except ValueError as error:
        raise ValueError("Enter a valid IPv4 or IPv6 address") from error

    return value


def validate_device(data):
    location = str(data.get("location", "")).strip()
    name = str(data.get("name", "")).strip()
    ip = validate_ip(str(data.get("ip", "")))

    return {
        "location": location or "-",
        "name": name or "Unnamed Device",
        "ip": ip,
    }


def parse_devices_csv(csv_text):
    rows = []
    reader = csv.DictReader(csv_text.splitlines())

    if not reader.fieldnames:
        raise ValueError("CSV file is empty")

    normalized_fields = {field.strip().lower(): field for field in reader.fieldnames if field}

    location_key = normalized_fields.get("location")
    name_key = normalized_fields.get("name")
    ip_key = normalized_fields.get("ip")

    if not ip_key:
        raise ValueError("CSV must include an IP column")

    for line_number, row in enumerate(reader, start=2):
        ip = str(row.get(ip_key, "")).strip()

        if not ip:
            continue

        location = str(row.get(location_key, "")).strip() if location_key else ""
        name = str(row.get(name_key, "")).strip() if name_key else ""

        try:
            ip = validate_ip(ip)
        except ValueError as error:
            raise ValueError(f"Line {line_number}: {error}") from error

        rows.append(
            {
                "location": location or "-",
                "name": name or f"Device {len(rows) + 1}",
                "ip": ip,
            }
        )

    if not rows:
        raise ValueError("CSV file does not contain any valid devices")

    return rows


def preview_devices_csv(user_id, csv_text):
    imported = parse_devices_csv(csv_text)
    existing_ips = {device["ip"] for device in read_devices(user_id)}

    rows = []
    for device in imported:
        rows.append(
            {
                "location": device["location"],
                "name": device["name"],
                "ip": device["ip"],
                "status": "duplicate" if device["ip"] in existing_ips else "new",
            }
        )

    return {
        "total": len(rows),
        "new_count": sum(1 for row in rows if row["status"] == "new"),
        "duplicate_count": sum(1 for row in rows if row["status"] == "duplicate"),
        "rows": rows[:50],
    }


def import_devices(user_id, csv_text, mode="merge"):
    imported = parse_devices_csv(csv_text)
    existing = read_devices(user_id)

    if mode == "replace":
        write_devices(user_id, imported)
        return {
            "added": len(imported),
            "skipped": 0,
            "total": len(imported),
        }

    existing_ips = {device["ip"] for device in existing}
    added = 0
    skipped = 0

    for device in imported:
        if device["ip"] in existing_ips:
            skipped += 1
            continue

        existing.append(device)
        existing_ips.add(device["ip"])
        added += 1

    write_devices(user_id, existing)

    return {
        "added": added,
        "skipped": skipped,
        "total": len(existing),
    }


def export_devices_csv(user_id):
    devices = read_devices(user_id)
    lines = [",".join(DEVICE_HEADER)]

    for device in devices:
        location = device["location"].replace('"', '""')
        name = device["name"].replace('"', '""')
        lines.append(f'"{location}","{name}",{device["ip"]}')

    return "\n".join(lines) + "\n"


def bootstrap_tenants():
    auth.ensure_data_dir()
    database.run_migrations()
    auth.ensure_demo_user()

    demo_user = auth.find_user_by_email("demo@netwatch.local")

    if demo_user:
        migrate_legacy_data(demo_user["id"])
