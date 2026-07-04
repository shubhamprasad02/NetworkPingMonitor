# NetWatch Final Local Version

NetWatch is a local web portal for ping monitoring with:

- Login and signup
- Separate companies per account
- Separate devices, live status, reports, and settings per company
- Add/edit/remove devices from the website
- Import devices from CSV
- Export devices to CSV
- Download outage reports as CSV
- Live ping checks from the server
- Outage report generation after recovery

## Run

Open PowerShell in this folder:

```powershell
cd "C:\Users\Shubham\Documents\Ping Monitoring\netwatch_final"
python server.py
```

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
