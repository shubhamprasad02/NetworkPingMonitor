# Uptime Tools Final Local Version

**v1.3** — Replaced the old fixed-interval streak-based status check with a professional outage classification engine. Each device is now pinged independently every 5 seconds; a single failed ping triggers three 1-second verification retries before anything is confirmed. If a retry succeeds, it's logged as Temporary Packet Loss and nothing else happens. If all retries fail, it becomes a confirmed outage and switches to 1-second monitoring until recovery, then is classified by duration: under 60 seconds is a Minor Interruption (diagnostics only — no report, no email, no impact on availability), and 60 seconds or longer is a Verified Outage (saved, included in reports, reduces availability, sends an outage email with start/end/duration). Also added Network Fluctuation detection: if a device racks up 10+ Minor Interruptions in a rolling 30-minute window, one "Network Fluctuation Detected" email goes out (with a cooldown so it doesn't repeat for the same ongoing issue), recommending a look at the ISP connection, router, switch, Wi-Fi, or cabling.

**v1.2** — The live dashboard table used to fully rebuild itself every 5 seconds even when nothing changed, which is what made it feel flickery/jumpy. It now only updates the rows that actually changed (a status flip, new latency, etc.) and leaves everything else untouched, plus a smooth fade for status-color changes and new rows.

**v1.1** — Devices are now monitored by an independent background thread (every 30s) instead of only while a browser tab was open. This means incident logging, reports, and email/online alerts now keep working even if you close the dashboard or the browser tab is backgrounded — as long as the machine itself stays powered on and awake. (Actual OS sleep still fully pauses everything on the PC, including this — that part can only be fixed via power settings or running on an always-on machine.)

Uptime Tools is a local web portal for ping monitoring with:

- Login and signup
- Separate companies per account
- Separate devices, live status, reports, and settings per company
- Add/edit/remove devices from the website
- Import devices from CSV
- Export devices to CSV
- Download outage reports as CSV
- Live ping checks from the server
- Outage report generation after recovery

## What's New

- **Login with User ID or Email** — every account now gets a unique User ID at signup (auto-suggested or custom). Log in with either your email or User ID plus your password.
- **Profile tab** — replaces the old "Alerting" nav item. Shows your name, email, and User ID, plus two alert cards: Email Alerts (primary/secondary recipients + threshold) and Online Alerts (in-browser/toast notifications when a device changes state).
- **Animated branding** — the Uptime Tools logo with a draw-in/pulse animation that replays each time you land on the Dashboard, plus branded loading screens on login/signup and toast progress on CSV import/export and report downloads.
- **Mobile fixes** — the sidebar is now a slide-out drawer (tap the menu icon), and the Dashboard/Expanded view scroll properly on phones so the average/availability stats are no longer cut off.

## Run

Open PowerShell in this folder:

```powershell
cd "C:\Users\Shubham\Documents\Ping Monitoring\netwatch_final"
python server.py
```

Or just double-click `RUN_UPTIME_TOOLS.bat`.

Open:

```text
http://127.0.0.1:8080
```

## Signup

Create a new account from the Signup tab.

Each login has its own companies, devices, and reports.

## Add Devices From Website

1. Login.
2. Select a company from the top-right company dropdown.
3. Open Devices.
4. Click Add Device.
5. Fill Location, Device Name, IP Address.
6. Save.

## Add Devices From CSV

1. Login.
2. Select company.
3. Open Devices.
4. Click Import CSV.
5. Choose a `.csv` file.

Supported CSV headers:

```csv
Location,Name,IP
Office,Main Router,192.168.1.1
Internet,Google DNS,8.8.8.8
```

Also supported:

```csv
Location,Device Name,IP Address
Office,Main Router,192.168.1.1
```

## Data Storage

All app data is stored locally in:

```text
data\store.json
```

Do not delete this file unless you want to reset accounts and data.

## Important

This is a local company-PC version. For public SaaS hosting, you would next add HTTPS, production database, paid accounts, and deployment.
