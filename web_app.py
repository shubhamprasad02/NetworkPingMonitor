import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import auth
import ping_monitor
import tenant_storage


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

HOST = "127.0.0.1"
PORT = 8080


class NetWatchHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/me":
            self.handle_me()
            return

        user = self.require_user()
        if user is None:
            return

        if path == "/api/devices":
            self.send_json({"devices": tenant_storage.read_devices(user["id"])})
            return

        if path == "/api/devices/export":
            csv_text = tenant_storage.export_devices_csv(user["id"])
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                'attachment; filename="devices.csv"',
            )
            body = csv_text.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/reports":
            query = parse_qs(parsed.query)
            ip = query.get("ip", ["all"])[0]
            reports = tenant_storage.read_reports(user["id"])

            if ip != "all":
                reports = [report for report in reports if report["ip"] == ip]

            self.send_json({"reports": reports})
            return

        if path == "/api/status":
            devices = tenant_storage.read_devices(user["id"])
            rows = ping_monitor.get_status_rows(
                user["id"],
                [
                    {
                        "location": device["location"],
                        "name": device["name"],
                        "ip": device["ip"],
                    }
                    for device in devices
                ],
                tenant_storage.report_file(user["id"]),
                tenant_storage.read_alerts(user["id"]),
            )
            self.send_json({"devices": rows})
            return

        if path == "/api/settings":
            self.send_json({"settings": tenant_storage.read_settings(user["id"])})
            return

        if path == "/api/alerts":
            self.send_json({"alerts": tenant_storage.read_alerts(user["id"])})
            return

        self.send_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/signup":
            self.handle_signup()
            return

        if path == "/api/auth/login":
            self.handle_login()
            return

        if path == "/api/auth/logout":
            self.handle_logout()
            return

        if path == "/api/auth/change-password":
            self.handle_change_password()
            return

        user = self.require_user()
        if user is None:
            return

        if path == "/api/devices":
            try:
                device = tenant_storage.validate_device(self.read_json())
                devices = tenant_storage.read_devices(user["id"])
                duplicate = next((item for item in devices if item["ip"] == device["ip"]), None)

                if duplicate:
                    raise ValueError(f"A device with IP {device['ip']} already exists")

                devices.append(device)
                tenant_storage.write_devices(user["id"], devices)
                self.send_json(
                    {"ok": True, "devices": tenant_storage.read_devices(user["id"])},
                    status=201,
                )
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            return

        if path == "/api/devices/import":
            try:
                payload = self.read_json()
                csv_text = str(payload.get("csv", "")).strip()
                mode = str(payload.get("mode", "merge")).strip().lower()

                if mode not in {"merge", "replace"}:
                    raise ValueError("Import mode must be merge or replace")

                if not csv_text:
                    raise ValueError("CSV content is required")

                result = tenant_storage.import_devices(user["id"], csv_text, mode=mode)
                self.send_json(
                    {
                        "ok": True,
                        "result": result,
                        "devices": tenant_storage.read_devices(user["id"]),
                    }
                )
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            return

        if path == "/api/devices/import/preview":
            try:
                payload = self.read_json()
                csv_text = str(payload.get("csv", "")).strip()

                if not csv_text:
                    raise ValueError("CSV content is required")

                preview = tenant_storage.preview_devices_csv(user["id"], csv_text)
                self.send_json({"ok": True, "preview": preview})
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            return

        if path == "/api/settings":
            try:
                payload = self.read_json()
                settings = tenant_storage.read_settings(user["id"])
                company = str(payload.get("company", "")).strip()

                if company:
                    auth.update_user_company(user["id"], company)

                refresh_interval = int(payload.get("refresh_interval", settings["refresh_interval"]))

                if refresh_interval not in {2, 5, 10}:
                    raise ValueError("Refresh interval must be 2, 5, or 10 seconds")

                timezone = str(payload.get("timezone", settings["timezone"])).strip() or settings["timezone"]
                settings.update(
                    {
                        "timezone": timezone,
                        "refresh_interval": refresh_interval,
                    }
                )
                tenant_storage.save_settings(user["id"], settings)
                user = auth.find_user_by_id(user["id"])
                self.send_json({"ok": True, "settings": settings, "user": auth.public_user(user)})
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            return

        if path == "/api/alerts":
            try:
                payload = self.read_json()
                alerts = tenant_storage.read_alerts(user["id"])
                primary_email = str(payload.get("primary_email", alerts["primary_email"])).strip()
                additional_emails = str(payload.get("additional_emails", alerts["additional_emails"])).strip()
                long_outage_alert_seconds = int(
                    payload.get("long_outage_alert_seconds", alerts["long_outage_alert_seconds"])
                )
                send_short_outage_reports = bool(
                    payload.get("send_short_outage_reports", alerts["send_short_outage_reports"])
                )

                if long_outage_alert_seconds < 5:
                    raise ValueError("Long outage alert must be at least 5 seconds")

                alerts.update(
                    {
                        "primary_email": primary_email,
                        "additional_emails": additional_emails,
                        "long_outage_alert_seconds": long_outage_alert_seconds,
                        "send_short_outage_reports": send_short_outage_reports,
                    }
                )
                tenant_storage.save_alerts(user["id"], alerts)
                self.send_json({"ok": True, "alerts": alerts})
            except ValueError as error:
                self.send_json({"ok": False, "error": str(error)}, status=400)
            return

        self.send_error(404)

    def do_PUT(self):
        user = self.require_user()
        if user is None:
            return

        parts = self.path.strip("/").split("/")

        if len(parts) != 3 or parts[:2] != ["api", "devices"]:
            self.send_error(404)
            return

        try:
            index = int(unquote(parts[2]))
            device = tenant_storage.validate_device(self.read_json())
            devices = tenant_storage.read_devices(user["id"])

            if index < 0 or index >= len(devices):
                raise ValueError("Device not found")

            duplicate = next(
                (item for item_index, item in enumerate(devices) if item_index != index and item["ip"] == device["ip"]),
                None,
            )

            if duplicate:
                raise ValueError(f"A device with IP {device['ip']} already exists")

            devices[index] = device
            tenant_storage.write_devices(user["id"], devices)
            self.send_json({"ok": True, "devices": tenant_storage.read_devices(user["id"])})
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=400)

    def do_DELETE(self):
        user = self.require_user()
        if user is None:
            return

        parts = self.path.strip("/").split("/")

        if len(parts) != 3 or parts[:2] != ["api", "devices"]:
            self.send_error(404)
            return

        try:
            index = int(unquote(parts[2]))
            devices = tenant_storage.read_devices(user["id"])

            if index < 0 or index >= len(devices):
                raise ValueError("Device not found")

            devices.pop(index)
            tenant_storage.write_devices(user["id"], devices)
            self.send_json({"ok": True, "devices": tenant_storage.read_devices(user["id"])})
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=400)

    def handle_signup(self):
        try:
            payload = self.read_json()
            user = auth.create_user(
                payload.get("company", ""),
                payload.get("email", ""),
                payload.get("password", ""),
            )
            tenant_storage.ensure_tenant_files(user["id"])
            token = auth.create_session(user["id"])
            settings = tenant_storage.read_settings(user["id"])
            self.send_json(
                {
                    "ok": True,
                    "token": token,
                    "user": user,
                    "settings": settings,
                },
                status=201,
            )
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=400)

    def handle_login(self):
        try:
            payload = self.read_json()
            user = auth.authenticate_user(payload.get("email", ""), payload.get("password", ""))
            tenant_storage.ensure_tenant_files(user["id"])
            token = auth.create_session(user["id"])
            settings = tenant_storage.read_settings(user["id"])
            self.send_json(
                {
                    "ok": True,
                    "token": token,
                    "user": auth.public_user(user),
                    "settings": settings,
                }
            )
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=400)

    def handle_logout(self):
        token = self.get_token()
        auth.delete_session(token)
        self.send_json({"ok": True})

    def handle_me(self):
        user = self.get_current_user()

        if not user:
            self.send_json({"ok": True, "authenticated": False})
            return

        settings = tenant_storage.read_settings(user["id"])
        self.send_json(
            {
                "ok": True,
                "authenticated": True,
                "user": auth.public_user(user),
                "settings": settings,
            }
        )

    def handle_change_password(self):
        user = self.require_user()
        if user is None:
            return

        try:
            payload = self.read_json()
            auth.change_password(
                user["id"],
                payload.get("current_password", ""),
                payload.get("new_password", ""),
            )
            self.send_json({"ok": True})
        except ValueError as error:
            self.send_json({"ok": False, "error": str(error)}, status=400)

    def get_token(self):
        auth_header = self.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            return auth_header[7:].strip()

        return self.headers.get("X-Auth-Token", "").strip()

    def get_current_user(self):
        session = auth.get_session(self.get_token())

        if not session:
            return None

        return auth.find_user_by_id(session["user_id"])

    def require_user(self):
        user = self.get_current_user()

        if user:
            return user

        self.send_json({"ok": False, "error": "Authentication required"}, status=401)
        return None

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(length).decode("utf-8")
        return json.loads(raw_body or "{}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, request_path):
        if request_path == "/":
            request_path = "/index.html"

        safe_path = request_path.lstrip("/").replace("/", "\\")
        file_path = (WEB_DIR / safe_path).resolve()

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
    tenant_storage.bootstrap_tenants()

    server = ThreadingHTTPServer((HOST, PORT), NetWatchHandler)
    print(f"NetWatch is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
