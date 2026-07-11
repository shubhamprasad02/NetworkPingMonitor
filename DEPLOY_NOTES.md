# What changed in this fixed copy

## 🚨 Do this first — rotate your Gmail credentials
`email_config.json` had a real Gmail address and App Password committed in
plain text, and the same password was hardcoded as a fallback inside
`email_alerts.py`. If this was ever pushed to GitHub (even a since-deleted
commit), treat that password as burned:
1. Google Account → Security → App Passwords → revoke `pergrxpaonrdgclh`.
2. Generate a new App Password for a fresh alert-only Gmail account if possible.
3. Never put it in a file that gets committed — see below.

## 1. One backend, not two
Removed `web_app.py`, `auth.py`, `ping_monitor.py`, `tenant_storage.py` — an
older, abandoned version of the app that predates `server.py` + `database.py`
+ `email_alerts.py` (the version your README and `RUN_NETWATCH.bat` actually
point to). Keeping both in the repo meant edits to the old files silently did
nothing.

## 2. One web folder, not three
Removed `web/app-fixed.js`, `web/host-marker.txt`, and the nested `web/web/`
folder — stale duplicates of `index.html` / `app.js` / `styles.css`. Only
`web/index.html`, `web/app.js`, `web/styles.css` remain, and those are the
ones `server.py` actually serves.

## 3. Credentials now come from environment variables
`email_alerts.py` no longer has a hardcoded password. It reads, in order:
1. `SENDER_EMAIL`, `SENDER_PASSWORD`, `RECEIVER_EMAIL`, `SMTP_SERVER`, `SMTP_PORT` environment variables
2. `email_config.json` (now blank placeholders, and gitignored so it can hold
   real values locally without ever being committed)

**On Render:** open your service → Environment tab → add `SENDER_EMAIL`,
`SENDER_PASSWORD` (the new App Password), `RECEIVER_EMAIL`. If none are set,
the app still runs fine — it just skips sending alert emails and logs a
warning, instead of crashing.

## 4. PORT now reads from the environment
`server.py` used a hardcoded `PORT = 8080`. Render assigns its own port via
the `PORT` environment variable at runtime and routes external traffic to
it — a hardcoded port meant the service was unreachable. Now:
```python
PORT = int(os.environ.get("PORT", 8080))
```
Locally it still defaults to 8080, so nothing changes for you on Windows.

## 5. Ping now works without ICMP access
Render's containers don't ship the `ping` binary / don't allow raw ICMP
sockets, so the old ping-via-subprocess approach always reported every
device OFFLINE there. The app now checks once at startup whether real ping
works; if not, it falls back to a TCP-connect check against common ports
(80, 443, 22, 3389, 8080) to determine if a host is reachable. It's a good
approximation, not a perfect substitute for ICMP — devices that only respond
to ping and have literally nothing listening on any of those ports will
still show OFFLINE. If that matters for your specific devices, the reliable
fix is a paid host/VM with real ICMP access rather than a code change.

## 6. Database will still reset on Render's free tier
`DATA_DIR` is now configurable via an environment variable, but that alone
doesn't make storage persistent — Render's *free* web services still wipe
disk on every restart/deploy. To actually keep data between restarts you need
a **Render Persistent Disk** (a paid add-on): mount it at, say, `/var/data`,
then set the environment variable `DATA_DIR=/var/data` in the service. Until
you add that disk, expect logins/devices/history to reset on every redeploy —
that's a Render plan choice, not something fixable in code.

## 7. Mobile/tablet responsiveness
The CSS already had a reasonable foundation (scrollable tables, a stacking
layout under 980px). Added: a dedicated tablet breakpoint (641–1024px) for
the stats grid, and 44px-minimum tap targets on nav items, buttons, and
inputs on any screen 1024px wide or narrower, since the original was sized
for mouse clicks.

## Still your call, not changed automatically
- The `data/tenants/` folder (per-account CSV/JSON files) and the local
  `pingmonitor.db` were removed from this zip since they're local dev/test
  data, already gitignored, and shouldn't ship in a repo used for deployment.
  Nothing in the app logic depends on them being present — they get created
  fresh on first signup.

---

# New features (this update)

## 1. Automatic ping-history cleanup after 7 days
Second-by-second ping rows are the biggest thing in your database. Every
6 hours, a background job now checks for any calendar day older than 7 days
(configurable via the `PING_LOG_RETENTION_DAYS` environment variable) and:
1. Computes and permanently saves that day's totals — total checks,
   successful/failed counts, average latency, total outage time, and the
   session time-ranges — into a new `daily_summary` table.
2. Only then deletes the raw second-by-second rows for that day.

Your Today/Week/Month/Custom reports read from `daily_summary` automatically
once raw rows for a date are gone, so nothing else in the app is affected —
this was tested end-to-end (seeded 9-day-old raw data, ran the cleanup,
confirmed the export report showed identical numbers before and after the
raw rows were deleted).

## 2. Excel export — session breakdown for single-day reports
For "Today" or a Custom range where you pick just one date, the export now
lists each monitoring session separately — e.g. if you had the site open
1 PM–4 PM, closed it, then reopened 5 PM–11 PM, you'll see two rows:
"Session 1" and "Session 2", each with its own start/end time, total checks,
unsuccessful count, average latency, and outage duration — plus a "DAY TOTAL"
row combining both.

A new session is detected whenever there's a **15+ minute gap** between
pings (meaning the site was closed and reopened later). If you'd rather use
a different gap size, tell me and I'll change the `session_gap_minutes`
value in `database.py`.

## 3. Excel export — daily breakdown for Week/Month/Custom ranges
Week, Month, and any multi-day Custom range now show **one row per calendar
day** with: total checks, unsuccessful checks, average latency, total outage
time, and health %. A summary row at the bottom totals the whole period.
(Month used to bucket by week — it's now daily, matching what you asked for.)

## 4. Date filter on the Incident Log tab
The Incident Log page now has "From" / "To" date pickers next to the device
filter. Pick a single day, or a range, to narrow down outage incidents —
these use your phone/browser's native date picker, so they work well on
mobile too. Hit "Clear" to go back to showing everything.

