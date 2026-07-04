import html
import json
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "email_config.json"

DEFAULT_CONFIG = {
    "sender_email": "mailproject.alert@gmail.com",
    "sender_password": "pergrxpaonrdgclh",
    "receiver_email": "shubhamprashad007@gmail.com", 
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,
    "long_outage_alert_seconds": 30,
    "short_outage_monitor_seconds": 300,
}

def load_email_config():
    if not CONFIG_FILE.exists():
        return DEFAULT_CONFIG.copy()
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            config = json.load(file)
            return {**DEFAULT_CONFIG, **config}
    except Exception:
        return DEFAULT_CONFIG.copy()

EMAIL_CONFIG = load_email_config()
SENDER_EMAIL = EMAIL_CONFIG["sender_email"]
SENDER_PASSWORD = EMAIL_CONFIG["sender_password"].replace(" ", "")
SMTP_SERVER = EMAIL_CONFIG["smtp_server"]
SMTP_PORT = int(EMAIL_CONFIG["smtp_port"])
LONG_OUTAGE_ALERT_SECONDS = int(EMAIL_CONFIG["long_outage_alert_seconds"])
SHORT_OUTAGE_MONITOR_SECONDS = int(EMAIL_CONFIG.get("short_outage_monitor_seconds", 300))


def get_long_outage_seconds(alert_config=None):
    """Return the long-outage threshold in seconds.
    Uses per-tenant alert_config when available, otherwise falls back to global config."""
    if alert_config and "long_outage_alert_seconds" in alert_config:
        return int(alert_config["long_outage_alert_seconds"])
    return LONG_OUTAGE_ALERT_SECONDS

def resolve_recipient_email(recipient):
    if not recipient:
        return EMAIL_CONFIG.get("receiver_email")
        
    if isinstance(recipient, dict):
        for key in ["receivers", "receiver_email", "primary_email", "email"]:
            val = recipient.get(key)
            if val:
                if key == "primary_email" and recipient.get("additional_emails"):
                    add = str(recipient.get("additional_emails")).strip()
                    if add:
                        return f"{str(val).strip()}, {add}"
                return str(val).strip()
        return EMAIL_CONFIG.get("receiver_email")
        
    return str(recipient).strip()

def send_email(subject, html_content, recipient):
    """Establishes connection to SMTP and sends alert logs out to the specific recipient."""
    recipient_email = resolve_recipient_email(recipient)
    if not recipient_email:
        print("[Email Engine] ABORTED: No recipient profile email found.")
        return False
        
    print(f"[Email Engine] Attempting to dispatch email alert: '{subject}' to {recipient_email}...")
    
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        print("[Email Engine] ABORTED: Sender credentials are empty or missing inside configurations.")
        return False

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = SENDER_EMAIL
    message["To"] = recipient_email
    message.set_content("Please enable HTML visibility inside your mail reader to look over this log.")
    message.add_alternative(html_content, subtype="html")

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(message)
        print(f"[Email Engine] SUCCESS: Mail notification dispatched cleanly.")
        return True
    except Exception as error:
        print(f"[Email Engine] CRITICAL FAILURE: Could not forward email message via SMTP. Reason: {error}")
        return False

def build_table(headers, rows):
    header_html = "".join(f"<th style='border: 1px solid #ddd; padding: 12px; background-color: #f2f2f2; text-align: left;'>{html.escape(str(h))}</th>" for h in headers)
    rows_html = []
    for row in rows:
        row_html = "".join(f"<td style='border: 1px solid #ddd; padding: 12px; text-align: left;'>{html.escape(str(cell))}</td>" for cell in row)
        rows_html.append(f"<tr>{row_html}</tr>")
    return f"<table style='width: 100%; border-collapse: collapse; margin-top: 15px; font-family: Arial, sans-serif;'><thead><tr>{header_html}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"

def send_long_outage_alert(device_info, ip_address, checked_at, recipient=None, alert_config=None):
    if not recipient and isinstance(device_info, dict) and "recipient" in device_info:
        recipient = device_info["recipient"]
    subject = f"🔴 CRITICAL OUTAGE: {device_info['name']} is OFFLINE"
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #ecc; background-color: #fff5f5; max-width: 600px;">
        <h2 style="color: #d9534f; margin-top: 0;">Device Outage Detected</h2>
        <p>The following infrastructure node has dropped offline and requires attention:</p>
        {build_table(["Metric Parameter", "Value State"], [
            ["Device Name", device_info["name"]],
            ["IP Address", ip_address],
            ["Location Context", device_info["location"]],
            ["Offline Since Time", device_info["time"].strftime("%d-%m-%Y %H:%M:%S")],
            ["Last Evaluated Check", checked_at.strftime("%d-%m-%Y %H:%M:%S")]
        ])}
    </div>
    """
    return send_email(subject, html_body, recipient)

def send_short_outage_report(outage, recipient=None, alert_config=None):
    if not recipient and isinstance(outage, dict) and "recipient" in outage:
        recipient = outage["recipient"]
    subject = f"🟢 RECOVERY REPORT: {outage['name']} is ONLINE"
    
    html_body = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #cee; background-color: #f5fff5; max-width: 600px;">
        <h2 style="color: #5cb85c; margin-top: 0;">Device Connection Recovered</h2>
        <p>The monitoring target has successfully answered ping requests:</p>
        {build_table(["Parameter Field", "Value Data"], [
            ["Device Name", outage["name"]],
            ["IP Address", outage["ip"]],
            ["Location Context", outage["location"]],
            ["Offline Time", outage["offline_time"].strftime("%d-%m-%Y %H:%M:%S")],
            ["Online Recovery Time", outage["online_time"].strftime("%d-%m-%Y %H:%M:%S")],
            ["Total Calculated Downtime", outage["downtime"]]
        ])}
    </div>
    """
    return send_email(subject, html_body, recipient)

def send_fluctuation_report(device, outages, recipient=None, alert_config=None):
    """Sends a fluctuation (repeated short outage) summary report."""
    if not recipient and isinstance(device, dict) and "recipient" in device:
        recipient = device["recipient"]
    subject = f"⚠️ FLUCTUATION ALERT: {device['name']} had {len(outages)} outage(s)"

    rows = [
        [
            outage.get("location", "-"),
            outage.get("name", "-"),
            outage.get("ip", "-"),
            outage["offline_time"].strftime("%H:%M:%S") if isinstance(outage.get("offline_time"), object) and hasattr(outage.get("offline_time"), "strftime") else str(outage.get("offline_time", "-")),
            outage["online_time"].strftime("%H:%M:%S") if isinstance(outage.get("online_time"), object) and hasattr(outage.get("online_time"), "strftime") else str(outage.get("online_time", "-")),
            outage.get("downtime", "-"),
        ]
        for outage in outages
    ]

    html_body = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; border: 1px solid #f0ad4e; background-color: #fffbf0; max-width: 650px;">
        <h2 style="color: #e67e22; margin-top: 0;">Network Fluctuation Detected</h2>
        <p>The device <strong>{html.escape(device['name'])}</strong> at
        <strong>{html.escape(device.get('location', '-'))}</strong>
        experienced <strong>{len(outages)}</strong> outage event(s) in a short window:</p>
        {build_table(
            ["Location", "Device", "IP Address", "Offline Time", "Online Time", "Downtime"],
            rows
        )}
    </div>
    """
    return send_email(subject, html_body, recipient)