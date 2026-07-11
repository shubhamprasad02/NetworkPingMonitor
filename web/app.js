const authView = document.querySelector("#authView");
const appView = document.querySelector("#appView");
const authMessage = document.querySelector("#authMessage");
const userLabel = document.querySelector("#userLabel");
const pageTitle = document.querySelector("#pageTitle");
const companySelect = document.querySelector("#companySelect");
const lastUpdated = document.querySelector("#lastUpdated");
const statusRows = document.querySelector("#statusRows");
const deviceCards = document.querySelector("#deviceCards");
const companyCards = document.querySelector("#companyCards");
const reportRows = document.querySelector("#reportRows");
const deviceDropdownList = document.querySelector("#deviceDropdownList");
const deviceFilterToggle = document.querySelector("#deviceFilterToggle");
const deviceFilterLabel = document.querySelector("#deviceFilterLabel");
const incidentResultCount = document.querySelector("#incidentResultCount");
const incidentStartDate = document.querySelector("#incidentStartDate");
const incidentEndDate = document.querySelector("#incidentEndDate");
const incidentDateClearBtn = document.querySelector("#incidentDateClearBtn");
const dashboardOverlay = document.querySelector("#dashboardOverlay");
const fsStatusRows = document.querySelector("#fsStatusRows");
const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8080" : "";

// ── Bar Tooltip ──────────────────────────────────────────────────────────────
let barTooltip = document.querySelector("#barTooltip");
if (!barTooltip) {
  barTooltip = document.createElement("div");
  barTooltip.id = "barTooltip";
  barTooltip.className = "bar-tooltip hidden";
  barTooltip.innerHTML = `
    <div style="position:absolute;bottom:-5px;left:50%;transform:translateX(-50%);width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-top:5px solid rgba(15,23,42,0.96);"></div>
    <div class="tooltip-time" style="font-weight:600;border-bottom:1px solid rgba(255,255,255,0.15);padding-bottom:4px;margin-bottom:6px;font-family:'Roboto Mono',monospace;font-size:11px;color:#94a3b8;">Time: --:--</div>
    <div style="display:flex;justify-content:space-between;gap:12px;align-items:center;min-width:140px;">
      <span style="color:#cbd5e1;font-weight:500;">Uptime:</span>
      <strong class="tooltip-health-val" style="font-size:14px;font-family:'Roboto Mono',monospace;">100%</strong>
    </div>
    <div class="tooltip-pings" style="font-size:10.5px;color:#cbd5e1;margin-top:4px;font-family:'Roboto Mono',monospace;">--</div>
  `;
  document.body.appendChild(barTooltip);
}

function showBarTooltip(element, bar) {
  if (bar.empty) return;
  const rect = element.getBoundingClientRect();
  barTooltip.querySelector(".tooltip-time").textContent = `Time: ${bar.time}`;
  const healthValEl = barTooltip.querySelector(".tooltip-health-val");
  healthValEl.textContent = `${bar.value}%`;
  healthValEl.style.color = healthColorSelector(bar.value);
  barTooltip.querySelector(".tooltip-pings").textContent = `${bar.successes} of ${bar.attempts} pings passed`;
  barTooltip.style.left = `${rect.left + rect.width / 2 + window.scrollX}px`;
  barTooltip.style.top = `${rect.top + window.scrollY}px`;
  barTooltip.classList.remove("hidden");
}

document.addEventListener("click", (e) => {
  if (!e.target.closest(".health-bar-wrap") && !e.target.closest(".ombar-wrap")) {
    barTooltip.classList.add("hidden");
  }
});

// ── State ────────────────────────────────────────────────────────────────────
let currentUser = null;
let companies = [];
let activeCompanyId = null;
let devices = [];
let statusDevices = [];
let reports = [];
let minorIncidents = [];
let deviceHistories = {}; // deviceId -> array of { time, value, attempts, successes }
let selectedDeviceId = null;
let activeReportIp = "all";
let reportStartDate = "";
let reportEndDate = "";
let editingDeviceId = null;
let editingCompanyId = null;
let notificationDirty = false;
let refreshTimer = null;
let pendingStatBoom = false; // true right after login, consumed once by renderDashboard

// Overlay state
let overlayChartInstance = null;
let overlayHourlyChartInstance = null;
let ovActiveTimeframe = "today";

const datalabelsPlugin = {
  id: 'datalabels',
  afterDatasetsDraw(chart) {
    const { ctx } = chart;
    ctx.save();
    chart.data.datasets.forEach((dataset, i) => {
      const meta = chart.getDatasetMeta(i);
      meta.data.forEach((bar, index) => {
        const val = dataset.data[index];
        if (val !== null && val !== undefined) {
          ctx.fillStyle = '#182033'; // var(--text)
          ctx.font = 'bold 9px Outfit, sans-serif';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'bottom';
          let yPos = bar.y - 4;
          if (yPos < chart.chartArea.top + 15) {
            yPos = bar.y + 12;
            ctx.fillStyle = '#ffffff';
          }
          ctx.fillText(`${val}%`, bar.x, yPos);
        }
      });
    });
    ctx.restore();
  }
};

// ── Utilities ────────────────────────────────────────────────────────────────
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

// ── Loading overlay (auth actions, boot) ────────────────────────────────────
const loadingOverlay = document.querySelector("#loadingOverlay");
const loadingOverlayText = document.querySelector("#loadingOverlayText");
function showLoadingOverlay(text) {
  if (!loadingOverlay) return;
  if (loadingOverlayText && text) loadingOverlayText.textContent = text;
  loadingOverlay.classList.remove("hidden", "fading");
}
function hideLoadingOverlay() {
  if (!loadingOverlay) return;
  loadingOverlay.classList.add("fading");
  setTimeout(() => loadingOverlay.classList.add("hidden"), 250);
}

// ── Toasts (upload/download/generic progress) ───────────────────────────────
const toastStack = document.querySelector("#toastStack");
function showToast({ title, sub = "", type = "progress", id = null } = {}) {
  if (!toastStack) return null;
  const toastId = id || `toast_${Math.random().toString(36).slice(2)}`;
  let el = document.getElementById(toastId);
  if (!el) {
    el = document.createElement("div");
    el.id = toastId;
    el.className = "toast";
    toastStack.appendChild(el);
  }
  el.className = `toast ${type}`;
  const icon = type === "success"
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`
    : type === "error"
      ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`
      : `<span class="toast-spinner"></span>`;
  el.innerHTML = `
    <span class="toast-icon">${icon}</span>
    <span class="toast-body">
      <span class="toast-title">${escapeHtml(title || "")}</span>
      ${sub ? `<span class="toast-sub">${escapeHtml(sub)}</span>` : ""}
      <span class="toast-progress-track" style="display:none;"><span class="toast-progress-fill" style="width:0%;"></span></span>
    </span>`;
  return toastId;
}
function updateToastProgress(toastId, pct, sub) {
  const el = document.getElementById(toastId);
  if (!el) return;
  const track = el.querySelector(".toast-progress-track");
  const fill = el.querySelector(".toast-progress-fill");
  const subEl = el.querySelector(".toast-sub");
  if (track) track.style.display = "block";
  if (fill) fill.style.width = `${Math.min(100, Math.max(0, pct))}%`;
  if (sub && subEl) subEl.textContent = sub;
}
function finishToast(toastId, { title, sub = "", type = "success", autoRemoveMs = 2200 } = {}) {
  const el = document.getElementById(toastId);
  if (!el) return;
  el.className = `toast ${type}`;
  const icon = type === "success"
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
  el.innerHTML = `
    <span class="toast-icon">${icon}</span>
    <span class="toast-body">
      <span class="toast-title">${escapeHtml(title || "")}</span>
      ${sub ? `<span class="toast-sub">${escapeHtml(sub)}</span>` : ""}
    </span>`;
  if (autoRemoveMs) {
    setTimeout(() => {
      el.classList.add("toast-out");
      setTimeout(() => el.remove(), 220);
    }, autoRemoveMs);
  }
}
// Simulates a smooth progress fill for actions that don't report real
// byte-level progress (CSV parse, PNG/Excel generation) so uploads and
// downloads feel alive instead of freezing the UI with no feedback.
function withProgressToast(title, task) {
  const id = showToast({ title, sub: "Starting…", type: "progress" });
  let pct = 8;
  const tick = setInterval(() => {
    pct = Math.min(90, pct + Math.random() * 18);
    updateToastProgress(id, pct, pct < 40 ? "Reading…" : pct < 75 ? "Processing…" : "Almost done…");
  }, 220);
  return Promise.resolve()
    .then(task)
    .then((result) => {
      clearInterval(tick);
      updateToastProgress(id, 100, "Done");
      finishToast(id, { title: `${title} — done`, type: "success" });
      return result;
    })
    .catch((err) => {
      clearInterval(tick);
      finishToast(id, { title: `${title} — failed`, sub: err.message || "", type: "error" });
      throw err;
    });
}

function activeCompany() {
  return companies.find((c) => c.id === activeCompanyId);
}

function healthColorSelector(val) {
  if (val === null || val === undefined) return "#94a3b8";
  let r, g, b;
  if (val >= 50) {
    const pct = (val - 50) / 50;
    r = Math.round(234 + (16 - 234) * pct);
    g = Math.round(179 + (185 - 179) * pct);
    b = Math.round(8 + (129 - 8) * pct);
  } else {
    const pct = val / 50;
    r = Math.round(239 + (234 - 239) * pct);
    g = Math.round(68 + (179 - 68) * pct);
    b = Math.round(68 + (8 - 68) * pct);
  }
  return `rgb(${r}, ${g}, ${b})`;
}

function badgeClass(status) {
  if (status === "ONLINE") return "online";
  if (status === "OFFLINE") return "offline";
  return "checking";
}

function statusSort(status) {
  if (status === "OFFLINE") return 0;
  if (status?.startsWith("CHECK")) return 1;
  return 2;
}

// ── Data Loading ─────────────────────────────────────────────────────────────
async function loadAll() {
  try {
    const companyData = await api("/api/companies");
    companies = companyData.companies || [];
    if (!activeCompanyId && companies.length) activeCompanyId = companies[0].id;
    if (!companies.find((c) => c.id === activeCompanyId) && companies.length) activeCompanyId = companies[0].id;
    if (!activeCompanyId) return renderAll();

    const [deviceData, majorData, minorData, statusData] = await Promise.all([
      api(`/api/companies/${activeCompanyId}/devices`),
      api(`/api/companies/${activeCompanyId}/incidents?type=major${reportsQueryString() ? "&" + reportsQueryString().slice(1) : ""}`),
      api(`/api/companies/${activeCompanyId}/incidents?type=minor${reportsQueryString() ? "&" + reportsQueryString().slice(1) : ""}`),
      api(`/api/companies/${activeCompanyId}/status`),
    ]);
    devices        = deviceData.devices   || [];
    reports        = majorData.incidents  || [];
    minorIncidents = minorData.incidents  || [];
    statusDevices  = statusData.devices   || [];
    checkOnlineAlerts(statusDevices);

    if (!selectedDeviceId && statusDevices.length) {
      selectedDeviceId = statusDevices[0].id;
    }

    updateUptimeHistory();
    renderAll();

    // Sync overlay if open
    if (dashboardOverlay && dashboardOverlay.classList.contains("open")) {
      renderOverlayDeviceList();
      renderOverlayStats();
      if (fsStatusRows) fsStatusRows.innerHTML = buildFsStatusRowsHTML();
    }
  } catch (err) {
    console.error("Error in loadAll:", err);
  }
}

// ── Uptime History (10-minute buckets, keep last 12 = 2 hours) ────────────
function updateUptimeHistory() {
  statusDevices.forEach((device) => {
    const isOnline = device.confirmedStatus === "ONLINE" || device.status === "ONLINE";
    if (!deviceHistories[device.id]) deviceHistories[device.id] = [];

    const now = new Date();
    const roundedMin = Math.floor(now.getMinutes() / 10) * 10;
    const nowLabel = `${now.getHours().toString().padStart(2, "0")}:${roundedMin.toString().padStart(2, "0")}`;
    const history = deviceHistories[device.id];
    const last = history[history.length - 1];

    if (last && last.time === nowLabel) {
      last.attempts++;
      if (isOnline) last.successes++;
      last.value = Math.round((last.successes / last.attempts) * 100);
    } else {
      history.push({ time: nowLabel, value: isOnline ? 100 : 0, attempts: 1, successes: isOnline ? 1 : 0 });
      if (history.length > 12) history.shift(); // keep last 12 bars (2 hours)
    }
  });
}

// ── App Show/Hide ─────────────────────────────────────────────────────────────
function showApp() {
  authView.classList.add("hidden");
  appView.classList.remove("hidden");
  userLabel.textContent = currentUser?.email || "Portal";
  const avatarInitial = document.querySelector("#topbarAvatarInitial");
  if (avatarInitial) {
    const source = currentUser?.name || currentUser?.email || "?";
    avatarInitial.textContent = source.trim().charAt(0).toUpperCase();
  }
  replayLogoAnimation();
  pendingStatBoom = true;
  loadAll();
  if (!refreshTimer) refreshTimer = setInterval(refreshIfIdle, 5000);
}

function showAuth() {
  appView.classList.add("hidden");
  authView.classList.remove("hidden");
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

function refreshIfIdle() {
  if (editingDeviceId || editingCompanyId || notificationDirty) return;
  // Don't let the silent 5s background refresh redraw the incident table
  // out from under the user while they're mid multi-select.
  if (selectedIncidentIds.size > 0) return;
  loadAll();
}

// ── "Boom" count-up: numbers rush from 0 up to their real value with a pop.
// Used once, right after login, so the dashboard feels alive as it loads.
function animateCountUp(el, endValue, { suffix = "", duration = 900 } = {}) {
  if (!el) return;
  const target = Number(endValue) || 0;
  el.classList.remove("stat-boom");
  void el.offsetWidth; // restart animation if it's already mid-run
  el.classList.add("stat-boom");
  const startTime = performance.now();

  function tick(now) {
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = progress === 1 ? 1 : 1 - Math.pow(2, -10 * progress); // easeOutExpo
    const current = Math.round(target * eased);
    el.textContent = `${current}${suffix}`;
    if (progress < 1) {
      requestAnimationFrame(tick);
    } else {
      el.textContent = `${target}${suffix}`;
      setTimeout(() => el.classList.remove("stat-boom"), 400);
    }
  }
  requestAnimationFrame(tick);
}

// ── MAIN DASHBOARD RENDER ────────────────────────────────────────────────────
function renderDashboard() {
  const online = statusDevices.filter((d) => d.confirmedStatus === "ONLINE" || d.status === "ONLINE").length;
  const offline = statusDevices.filter((d) => d.confirmedStatus === "OFFLINE" || d.status === "OFFLINE").length;

  if (pendingStatBoom) {
    animateCountUp(document.querySelector("#totalDevices"), statusDevices.length);
    animateCountUp(document.querySelector("#onlineDevices"), online);
    animateCountUp(document.querySelector("#offlineDevices"), offline);
    animateCountUp(document.querySelector("#reportCount"), reports.length);
  } else {
    document.querySelector("#totalDevices").textContent = statusDevices.length;
    document.querySelector("#onlineDevices").textContent = online;
    document.querySelector("#offlineDevices").textContent = offline;
    document.querySelector("#reportCount").textContent = reports.length;
  }

  // Update auth preview stats if visible
  const authOnline = document.querySelector("#authOnline");
  if (authOnline) authOnline.textContent = online;
  const authOffline = document.querySelector("#authOffline");
  if (authOffline) authOffline.textContent = offline;

  renderMainStatusRows();

  renderHealthBars();
  renderNotificationFields();
  lastUpdated.textContent = `Synced ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

function buildOneStatusRowHTML(d) {
  return `<tr class="${selectedDeviceId === d.id ? "selected-device-row" : ""}" data-row-device-id="${d.id}" data-main-row>
        <td>${escapeHtml(d.location || "-")}</td>
        <td>${escapeHtml(d.name)}</td>
        <td>${escapeHtml(d.ip)}</td>
        <td><span class="badge ${badgeClass(d.confirmedStatus || d.status)}">${escapeHtml(d.confirmedStatus || d.status)}</span></td>
        <td>${escapeHtml(d.latency || d.ping || "—")}</td>
        <td>${escapeHtml(d.downCount || 0)}</td>
        <td>${escapeHtml(d.downFor || "—")}</td>
      </tr>`;
}

// Keyed diff render: reuses existing <tr> DOM nodes by device id and only
// touches the ones whose content actually changed. A full innerHTML
// rebuild every 5-second refresh was what made the dashboard feel
// flickery/jumpy — this keeps everything steady unless something real
// changed (a status flip, a new latency number, etc).
function renderMainStatusRows() {
  const sorted = statusDevices.slice().sort((a, b) => statusSort(a.status) - statusSort(b.status));

  if (!sorted.length) {
    if (!statusRows.querySelector(".empty-cell")) {
      statusRows.innerHTML = `<tr><td colspan="7" class="empty-cell">No active tracking devices.</td></tr>`;
    }
    return;
  }

  const existing = new Map();
  statusRows.querySelectorAll("tr[data-row-device-id]").forEach((tr) => existing.set(tr.dataset.rowDeviceId, tr));

  let previousEl = null;
  const seen = new Set();

  sorted.forEach((device) => {
    seen.add(device.id);
    const newRowHtml = buildOneStatusRowHTML(device);
    let tr = existing.get(device.id);

    if (!tr) {
      const tmp = document.createElement("tbody");
      tmp.innerHTML = newRowHtml;
      tr = tmp.firstElementChild;
      tr.classList.add("row-enter");
      requestAnimationFrame(() => tr.classList.remove("row-enter"));
    } else if (tr.dataset.rowHtml !== newRowHtml) {
      const tmp = document.createElement("tbody");
      tmp.innerHTML = newRowHtml;
      const fresh = tmp.firstElementChild;
      tr.className = fresh.className;
      tr.innerHTML = fresh.innerHTML;
    }
    tr.dataset.rowHtml = newRowHtml;

    const wantsPosition = previousEl ? previousEl.nextElementSibling : statusRows.firstElementChild;
    if (wantsPosition !== tr) {
      statusRows.insertBefore(tr, previousEl ? previousEl.nextElementSibling : statusRows.firstElementChild);
    }
    previousEl = tr;
  });

  existing.forEach((tr, id) => {
    if (!seen.has(id)) tr.remove();
  });

  const emptyCell = statusRows.querySelector(".empty-cell");
  if (emptyCell) emptyCell.closest("tr")?.remove();
}

// One delegated listener instead of re-attaching one per row on every
// refresh — clicking anywhere in a row selects that device.
statusRows.addEventListener("click", (e) => {
  const row = e.target.closest("[data-main-row]");
  if (!row) return;
  selectedDeviceId = row.dataset.rowDeviceId;
  renderDashboard();
});

// ── MAIN HEALTH BARS (6 bars) ─────────────────────────────────────────────────
function renderHealthBars() {
  const targetDevice = statusDevices.find((d) => d.id === selectedDeviceId);
  const history = selectedDeviceId ? (deviceHistories[selectedDeviceId] || []) : [];
  const currentPct = history.length ? history[history.length - 1].value : 0;

  const widgetTitle = document.querySelector("#chartDeviceTitle");
  if (widgetTitle) widgetTitle.textContent = targetDevice ? targetDevice.name : "Uptime History";

  const heroNum = document.querySelector("#currentHealth");
  if (heroNum) {
    if (pendingStatBoom) {
      animateCountUp(heroNum, currentPct, { suffix: "%", duration: 1100 });
    } else {
      heroNum.textContent = `${currentPct}%`;
    }
    heroNum.style.color = healthColorSelector(currentPct);
  }
  pendingStatBoom = false;

  const container = document.querySelector("#healthBars");
  if (!container) return;

  const TOTAL_BARS = 12;
  const emptyBar = { time: "--:--", value: null, empty: true };
  const padded = Array(Math.max(0, TOTAL_BARS - history.length)).fill(null).map(() => ({ ...emptyBar }));
  const displayBars = [...padded, ...history];

  // Rebuild DOM if count doesn't match
  if (container.children.length !== TOTAL_BARS) {
    container.innerHTML = displayBars.map((bar, i) => {
      const isLive = i === TOTAL_BARS - 1;
      return `
        <div class="health-bar-wrap" data-index="${i}">
          <div class="health-bar-track">
            <div class="health-bar${isLive ? " health-bar-live" : ""}" style="height:0%;background-color:#e2e8f0;"></div>
          </div>
          <span class="health-label">--:--</span>
        </div>`;
    }).join("");
  }

  requestAnimationFrame(() => {
    displayBars.forEach((bar, i) => {
      const wrap = container.children[i];
      if (!wrap) return;
      const isLive = i === TOTAL_BARS - 1 && !bar.empty;

      if (!bar.empty) {
        wrap.onclick = (e) => { e.stopPropagation(); showBarTooltip(wrap, bar); };
      } else {
        wrap.onclick = null;
      }

      const barEl = wrap.querySelector(".health-bar");
      const labelEl = wrap.querySelector(".health-label");

      if (barEl) {
        if (bar.empty) {
          barEl.style.height = "4px";
          barEl.style.backgroundColor = "#e2e8f0";
          barEl.style.opacity = "0.5";
          barEl.classList.remove("health-bar-live");
          barEl.style.boxShadow = "none";
        } else {
          barEl.style.height = `${Math.max(bar.value, 6)}%`;
          barEl.style.backgroundColor = healthColorSelector(bar.value);
          barEl.style.opacity = "1";
          if (isLive) {
            barEl.style.boxShadow = `0 0 10px 2px ${healthColorSelector(bar.value)}55`;
            if (!barEl.classList.contains("health-bar-live")) barEl.classList.add("health-bar-live");
          } else {
            barEl.style.boxShadow = "none";
            barEl.classList.remove("health-bar-live");
          }
        }
      }
      if (labelEl) {
        labelEl.textContent = bar.empty ? "--:--" : bar.time;
        labelEl.style.color = bar.empty ? "#ccc" : (isLive ? healthColorSelector(bar.value) : "#888");
      }
    });
  });
}

// ── OTHER RENDER FUNCTIONS ────────────────────────────────────────────────────
function renderCompanySelect() {
  companySelect.innerHTML = companies.map((c) => `<option value="${c.id}" ${c.id === activeCompanyId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join("");
}

function renderDevices() {
  if (!devices.length) {
    deviceCards.innerHTML = `<div class="empty-card">No devices yet. Add one or import a CSV file.</div>`;
    return;
  }
  deviceCards.innerHTML = devices.map((device) => {
    if (editingDeviceId === device.id) {
      const isMuted = device.muted === 1 || device.muted === true;
      return `<article class="device-card">
        <form class="edit-device-form" data-id="${device.id}">
          <label>Location<input name="location" value="${escapeHtml(device.location)}" required /></label>
          <label>Device Name<input name="name" value="${escapeHtml(device.name)}" required /></label>
          <label>IP Address<input name="ip" value="${escapeHtml(device.ip)}" required /></label>
          <div class="mute-label-wrap">
            <span>Mute Alerts</span>
            <label class="switch" style="margin:0;display:inline-block;"><input type="checkbox" name="muted" ${isMuted ? "checked" : ""}><span class="slider"></span></label>
          </div>
          <div class="device-actions" style="margin-top:12px;">
            <button class="small-action" type="submit">Save</button>
            <button class="small-action danger" type="button" data-cancel-device>Cancel</button>
          </div>
        </form>
      </article>`;
    }
    const status = statusDevices.find((item) => item.id === device.id)?.status || "UNKNOWN";
    const isMuted = device.muted === 1 || device.muted === true;
    return `<article class="device-card">
      <div class="device-card-header">
        <strong style="margin:0;">${escapeHtml(device.name)}</strong>
        ${isMuted ? '<span class="badge muted" title="Alerts muted">🔕 Muted</span>' : ""}
      </div>
      <p style="margin-top:8px;margin-bottom:10px;">${escapeHtml(device.location)}<br>${escapeHtml(device.ip)}</p>
      <span class="badge ${badgeClass(status)}">${escapeHtml(status)}</span>
      <div class="device-actions">
        <button class="small-action" data-edit-device="${device.id}">Edit</button>
        <button class="small-action danger" data-remove-device="${device.id}">Remove</button>
      </div>
    </article>`;
  }).join("");
  bindDeviceActions();
}

function bindDeviceActions() {
  document.querySelectorAll("[data-edit-device]").forEach((btn) => btn.addEventListener("click", () => { editingDeviceId = btn.dataset.editDevice; renderDevices(); }));
  document.querySelectorAll("[data-cancel-device]").forEach((btn) => btn.addEventListener("click", () => { editingDeviceId = null; renderDevices(); }));
  document.querySelectorAll("[data-remove-device]").forEach((btn) => btn.addEventListener("click", async () => {
    const device = devices.find((d) => d.id === btn.dataset.removeDevice);
    if (!confirm(`Remove ${device.name} (${device.ip})?`)) return;
    await api(`/api/companies/${activeCompanyId}/devices/${device.id}`, { method: "DELETE" });
    editingDeviceId = null;
    if (selectedDeviceId === device.id) selectedDeviceId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-device-form").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const isMuted = form.querySelector('input[name="muted"]').checked ? 1 : 0;
    await api(`/api/companies/${activeCompanyId}/devices/${form.dataset.id}`, {
      method: "PUT",
      body: JSON.stringify({ location: fd.get("location"), name: fd.get("name"), ip: fd.get("ip"), muted: isMuted }),
    });
    editingDeviceId = null;
    await loadAll();
  }));
}

// ── Incident Log state ────────────────────────────────────────────────────
let activeIncidentTab = "major";   // "major" | "minor"
let selectedIncidentIds = new Set();

const majorTabBadge   = document.querySelector("#majorTabBadge");
const minorTabBadge   = document.querySelector("#minorTabBadge");
const tabHint         = document.querySelector("#tabHint");
const bulkActionBar   = document.querySelector("#bulkActionBar");
const bulkCount       = document.querySelector("#bulkCount");
const bulkMoveBtn     = document.querySelector("#bulkMoveBtn");
const bulkMoveTarget  = document.querySelector("#bulkMoveTarget");
const bulkCancelBtn   = document.querySelector("#bulkCancelBtn");
const selectAllChk    = document.querySelector("#selectAllIncidents");
const causeColHeader  = document.querySelector("#causeColHeader");

function incidentsQueryString() {
  const params = new URLSearchParams();
  if (activeIncidentTab)  params.set("type", activeIncidentTab);
  if (reportStartDate && reportEndDate && reportStartDate === reportEndDate) {
    params.set("date", reportStartDate);
  } else {
    if (reportStartDate) params.set("start", reportStartDate);
    if (reportEndDate)   params.set("end", reportEndDate);
  }
  if (activeReportIp && activeReportIp !== "all") params.set("device_id", activeReportIp);
  return `?${params.toString()}`;
}

// Keep old name for backwards compat (loadAll still uses it)
function reportsQueryString() {
  const params = new URLSearchParams();
  if (reportStartDate && reportEndDate && reportStartDate === reportEndDate) {
    params.set("date", reportStartDate);
  } else {
    if (reportStartDate) params.set("start", reportStartDate);
    if (reportEndDate)   params.set("end", reportEndDate);
  }
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

async function refreshReports() {
  if (!activeCompanyId) return;
  const [majorData, minorData] = await Promise.all([
    api(`/api/companies/${activeCompanyId}/incidents?type=major${reportsQueryString() ? "&" + reportsQueryString().slice(1) : ""}`),
    api(`/api/companies/${activeCompanyId}/incidents?type=minor${reportsQueryString() ? "&" + reportsQueryString().slice(1) : ""}`),
  ]);
  reports        = majorData.incidents || [];
  minorIncidents = minorData.incidents || [];
  renderReports();
}

function currentIncidents() {
  return activeIncidentTab === "major" ? reports : minorIncidents;
}

function renderReports() {
  // Preserve the user's current tick marks across re-renders (this function
  // also runs on every silent 5s background refresh). Only drop a selected
  // id once it genuinely no longer exists in this tab's incident list —
  // never wipe the whole selection just because the table redrew.
  const stillValidIds = new Set(currentIncidents().map((r) => r.id));
  [...selectedIncidentIds].forEach((id) => {
    if (!stillValidIds.has(id)) selectedIncidentIds.delete(id);
  });
  _updateBulkBar();
  renderReportDropdown();

  const allCurrent = currentIncidents();
  const filtered = activeReportIp === "all"
    ? allCurrent
    : allCurrent.filter((r) => r.ip === activeReportIp);

  // Tab badges
  if (majorTabBadge) majorTabBadge.textContent = reports.length;
  if (minorTabBadge) minorTabBadge.textContent = minorIncidents.length;

  // Tab hint
  if (tabHint) {
    tabHint.textContent = activeIncidentTab === "major"
      ? "Verified outages — shown in reports & exports"
      : "Short blips, our-internet drops & manually moved incidents";
  }

  // Show/hide the "Reason" column only on the Minor tab
  if (causeColHeader) causeColHeader.classList.toggle("hidden", activeIncidentTab !== "minor");

  if (incidentResultCount) {
    incidentResultCount.textContent = filtered.length === allCurrent.length
      ? `${allCurrent.length} incident${allCurrent.length !== 1 ? "s" : ""} total`
      : `${filtered.length} of ${allCurrent.length}`;
  }

  const targetVerb = activeIncidentTab === "major" ? "Move to Minor" : "Move to Major";
  const isMinorTab = activeIncidentTab === "minor";

  reportRows.innerHTML = filtered.length
    ? filtered.map((r) => {
        const causePill = isMinorTab ? _causePill(r.cause) : "";
        return `<tr data-incident-id="${escapeHtml(r.id)}">
          <td><input type="checkbox" class="incident-chk" data-id="${escapeHtml(r.id)}" ${selectedIncidentIds.has(r.id) ? "checked" : ""}></td>
          <td>${escapeHtml(r.date)}</td>
          <td>${escapeHtml(r.location || "—")}</td>
          <td><span class="device-tag">${escapeHtml(r.name || "—")}</span></td>
          <td>${escapeHtml(r.ip || "—")}</td>
          <td>${escapeHtml(r.offline || "—")}</td>
          <td>${escapeHtml(r.online || "—")}</td>
          <td>${escapeHtml(r.downtime || r.duration_seconds ? (r.downtime || r.duration_seconds + "s") : "—")}</td>
          ${isMinorTab ? `<td>${causePill}</td>` : ""}
          <td><button class="reclass-btn" data-reclass-id="${escapeHtml(r.id)}">${targetVerb}</button></td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="${isMinorTab ? 10 : 9}" class="empty-cell">No ${activeIncidentTab} incidents for this filter.</td></tr>`;

  // Bind per-row reclassify buttons
  document.querySelectorAll(".reclass-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.reclassId;
      const target = activeIncidentTab === "major" ? "minor" : "major";
      await _reclassifyIncidents([id], target);
    });
  });

  // Bind per-row checkboxes
  const allChkEls = document.querySelectorAll(".incident-chk");
  allChkEls.forEach((chk) => {
    chk.addEventListener("change", () => {
      if (chk.checked) selectedIncidentIds.add(chk.dataset.id);
      else selectedIncidentIds.delete(chk.dataset.id);
      _updateBulkBar();
      if (selectAllChk) {
        const allChks = document.querySelectorAll(".incident-chk");
        selectAllChk.checked = allChks.length > 0 && [...allChks].every((c) => c.checked);
        selectAllChk.indeterminate = !selectAllChk.checked && selectedIncidentIds.size > 0;
      }
    });
  });

  // Sync the select-all checkbox immediately after (re-)render so it reflects
  // the preserved selection right away, not just after the next click.
  if (selectAllChk) {
    selectAllChk.checked = allChkEls.length > 0 && [...allChkEls].every((c) => c.checked);
    selectAllChk.indeterminate = !selectAllChk.checked && selectedIncidentIds.size > 0;
  }
}

function _causePill(cause) {
  if (cause === "self_internet") return `<span class="cause-pill internet">📶 Our internet</span>`;
  if (cause === "manual")        return `<span class="cause-pill manual">👤 Moved manually</span>`;
  return `<span class="cause-pill short">⚡ Short blip</span>`;
}

function _updateBulkBar() {
  const n = selectedIncidentIds.size;
  if (!bulkActionBar) return;
  if (n === 0) {
    bulkActionBar.classList.add("hidden");
  } else {
    bulkActionBar.classList.remove("hidden");
    if (bulkCount)      bulkCount.textContent   = `${n} selected`;
    if (bulkMoveTarget) bulkMoveTarget.textContent = activeIncidentTab === "major" ? "Minor" : "Major";
  }
}

async function _reclassifyIncidents(ids, target) {
  if (!ids.length) return;
  const label = target === "major" ? "Major" : "Minor";
  const confirmed = ids.length === 1
    ? true
    : confirm(`Move ${ids.length} incidents to ${label}?`);
  if (!confirmed) return;

  let failed = 0;
  await Promise.all(ids.map(async (id) => {
    try {
      await api(`/api/companies/${activeCompanyId}/incidents/${id}/reclassify`, {
        method: "POST",
        body: JSON.stringify({ target }),
      });
    } catch { failed++; }
  }));

  if (failed) showToast(`${failed} incident(s) could not be moved.`, "warn");
  selectedIncidentIds.clear();
  await refreshReports();
  renderReports();
}

function renderReportDropdown() {
  if (!deviceDropdownList) return;
  const allIncidents = [...reports, ...minorIncidents];
  const known = new Map();
  devices.forEach((d) => known.set(d.ip, { name: d.name, location: d.location }));
  allIncidents.forEach((r) => { if (!known.has(r.ip)) known.set(r.ip, { name: r.name, location: r.location }); });

  const curr = currentIncidents();
  const allCount = curr.length;
  let items = `
    <div class="device-dropdown-item ${activeReportIp === "all" ? "selected" : ""}" data-dd-ip="all">
      <div class="ddi-left"><span class="ddi-name">All Devices</span><span class="ddi-location">Show all incidents</span></div>
      <span class="ddi-count">${allCount}</span>
    </div>
    <hr class="ddi-separator">`;

  [...known.entries()].forEach(([ip, info]) => {
    const count = curr.filter((r) => r.ip === ip).length;
    items += `
      <div class="device-dropdown-item ${activeReportIp === ip ? "selected" : ""}" data-dd-ip="${escapeHtml(ip)}">
        <div class="ddi-left"><span class="ddi-name">${escapeHtml(info.name)}</span><span class="ddi-location">${escapeHtml(info.location)}</span></div>
        <span class="ddi-count">${count}</span>
      </div>`;
  });

  deviceDropdownList.innerHTML = items;
  if (deviceFilterLabel) {
    if (activeReportIp === "all") {
      deviceFilterLabel.textContent = "All Devices";
    } else {
      const info = known.get(activeReportIp);
      deviceFilterLabel.textContent = info ? info.name : activeReportIp;
    }
  }

  document.querySelectorAll("[data-dd-ip]").forEach((item) => {
    item.addEventListener("click", () => {
      activeReportIp = item.dataset.ddIp;
      closeDeviceDropdown();
      renderReports();
    });
  });
}

// ── Tab switching ────────────────────────────────────────────────────────
document.querySelectorAll(".incident-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    activeIncidentTab = tab.dataset.tab;
    document.querySelectorAll(".incident-tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    selectedIncidentIds.clear();
    renderReports();
  });
});

// ── Select-all checkbox ──────────────────────────────────────────────────
if (selectAllChk) {
  selectAllChk.addEventListener("change", () => {
    const checked = selectAllChk.checked;
    document.querySelectorAll(".incident-chk").forEach((chk) => {
      chk.checked = checked;
      if (checked) selectedIncidentIds.add(chk.dataset.id);
      else selectedIncidentIds.delete(chk.dataset.id);
    });
    _updateBulkBar();
  });
}

// ── Bulk action bar buttons ──────────────────────────────────────────────
if (bulkMoveBtn) {
  bulkMoveBtn.addEventListener("click", async () => {
    const target = activeIncidentTab === "major" ? "minor" : "major";
    await _reclassifyIncidents([...selectedIncidentIds], target);
  });
}
if (bulkCancelBtn) {
  bulkCancelBtn.addEventListener("click", () => {
    selectedIncidentIds.clear();
    document.querySelectorAll(".incident-chk").forEach((c) => { c.checked = false; });
    if (selectAllChk) { selectAllChk.checked = false; selectAllChk.indeterminate = false; }
    _updateBulkBar();
  });
}

function renderCompanies() {
  companyCards.innerHTML = companies.map((company) => {
    if (editingCompanyId === company.id) {
      return `<article class="company-card active"><form class="edit-company-form" data-id="${company.id}"><label>Company Name<input name="name" value="${escapeHtml(company.name)}" required /></label><label>Contact Email<input name="email" type="email" value="${escapeHtml(company.receivers)}" required /></label><div class="device-actions"><button class="small-action" type="submit">Save</button><button class="small-action danger" type="button" data-cancel-company>Cancel</button></div></form></article>`;
    }
    const count = company.id === activeCompanyId ? { devices: devices.length, reports: reports.length } : { devices: "—", reports: "—" };
    return `<article class="company-card ${company.id === activeCompanyId ? "active" : ""}"><div class="company-card-top"><div><strong>${escapeHtml(company.name)}</strong><p>${escapeHtml(company.receivers)}</p></div></div><div class="company-mini-stats"><span>${count.devices} devices</span><span>${count.reports} reports</span></div><div class="device-actions"><button class="small-action" data-open-company="${company.id}">Open</button><button class="small-action" data-edit-company="${company.id}">Edit</button><button class="small-action danger" data-remove-company="${company.id}">Remove</button></div></article>`;
  }).join("");
  bindCompanyActions();
}

function bindCompanyActions() {
  document.querySelectorAll("[data-open-company]").forEach((btn) => btn.addEventListener("click", async () => { activeCompanyId = btn.dataset.openCompany; activeReportIp = "all"; selectedDeviceId = null; await loadAll(); }));
  document.querySelectorAll("[data-edit-company]").forEach((btn) => btn.addEventListener("click", () => { editingCompanyId = btn.dataset.editCompany; renderCompanies(); }));
  document.querySelectorAll("[data-cancel-company]").forEach((btn) => btn.addEventListener("click", () => { editingCompanyId = null; renderCompanies(); }));
  document.querySelectorAll("[data-remove-company]").forEach((btn) => btn.addEventListener("click", async () => {
    const company = companies.find((c) => c.id === btn.dataset.removeCompany);
    if (!confirm(`Remove ${company.name}? All devices and reports will be removed.`)) return;
    await api(`/api/companies/${company.id}`, { method: "DELETE" });
    if (activeCompanyId === company.id) activeCompanyId = null;
    selectedDeviceId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-company-form").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    await api(`/api/companies/${form.dataset.id}`, { method: "PUT", body: JSON.stringify({ name: fd.get("name"), receivers: fd.get("email"), email: fd.get("email"), plan: "Starter" }) });
    editingCompanyId = null;
    await loadAll();
  }));
}

function renderNotificationFields() {
  const company = activeCompany();
  if (!company || notificationDirty) return;
  if (document.querySelector("#companyEmail")) document.querySelector("#companyEmail").value = company.email || company.receivers || "";
  if (document.querySelector("#companyReceivers")) document.querySelector("#companyReceivers").value = company.receivers || "";
  if (document.querySelector("#alertSeconds")) document.querySelector("#alertSeconds").value = company.alert_after_seconds || 30;
  const onlineToggle = document.querySelector("#onlineAlertsToggle");
  if (onlineToggle) onlineToggle.checked = company.online_alerts === undefined || company.online_alerts === null ? true : !!Number(company.online_alerts);
}

function renderProfile() {
  if (!currentUser) return;
  const nameEl = document.querySelector("#profileName");
  const emailEl = document.querySelector("#profileEmail");
  const usernameEl = document.querySelector("#profileUsername");
  if (nameEl) nameEl.textContent = currentUser.name || currentUser.company || "—";
  if (emailEl) emailEl.textContent = currentUser.email || "—";
  if (usernameEl) usernameEl.textContent = currentUser.username || "—";
  renderNotificationFields();
  renderAlertHistory();
}

// ── Logo animation replay ────────────────────────────────────────────────────
// Every time someone lands on the dashboard, briefly remove + re-add the
// replay class so the CSS draw-in/pulse animation restarts from scratch.
function replayLogoAnimation() {
  document.querySelectorAll(".np-logo").forEach((logo) => {
    logo.classList.remove("np-logo-replay");
    // eslint-disable-next-line no-unused-expressions
    logo.offsetWidth; // force reflow so the animation can restart
    logo.classList.add("np-logo-replay");
  });
}

// ── Online (in-browser) alerts ───────────────────────────────────────────────
// Compares each refresh against the previous snapshot and fires a toast +
// native browser notification (if permitted) the moment a device's status
// changes, independent of the email alert pipeline.
let previousDeviceStatuses = {};
function checkOnlineAlerts(newStatusDevices) {
  const company = activeCompany();
  const enabled = !company || company.online_alerts === undefined || company.online_alerts === null
    ? true
    : !!Number(company.online_alerts);

  newStatusDevices.forEach((device) => {
    const prevStatus = previousDeviceStatuses[device.id];
    const currStatus = device.confirmedStatus || device.status;
    if (prevStatus && prevStatus !== currStatus && (currStatus === "ONLINE" || currStatus === "OFFLINE")) {
      if (enabled) fireOnlineAlert(device, currStatus);
    }
    previousDeviceStatuses[device.id] = currStatus;
  });
}

function fireOnlineAlert(device, status) {
  const isOnline = status === "ONLINE";
  showToast({
    title: `${device.name} is ${isOnline ? "back online" : "offline"}`,
    sub: device.ip,
    type: isOnline ? "success" : "error",
  });
  // Auto-dismiss ad-hoc status toasts after a few seconds
  setTimeout(() => {
    document.querySelectorAll(".toast").forEach((t) => {
      if (t.querySelector(".toast-title")?.textContent?.includes(device.name)) {
        t.classList.add("toast-out");
        setTimeout(() => t.remove(), 220);
      }
    });
  }, 4500);

  if (window.Notification && Notification.permission === "granted") {
    new Notification(`Uptime Tools — ${device.name}`, {
      body: `${device.name} (${device.ip}) is now ${isOnline ? "online" : "offline"}.`,
    });
  }

  pushAlertHistory({ name: device.name, ip: device.ip, online: isOnline, ts: Date.now() });
}

// ══════════════════════════════════════════════════════════════════════════════
//  NOTIFICATION HISTORY (last 24h, persisted locally)
// ══════════════════════════════════════════════════════════════════════════════
const ALERT_HISTORY_KEY = "uptime_tools_alert_history";
const ALERT_HISTORY_WINDOW_MS = 24 * 60 * 60 * 1000;

function loadAlertHistory() {
  try {
    const raw = JSON.parse(localStorage.getItem(ALERT_HISTORY_KEY) || "[]");
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function pushAlertHistory(entry) {
  const cutoff = Date.now() - ALERT_HISTORY_WINDOW_MS;
  const list = loadAlertHistory().filter((e) => e.ts >= cutoff);
  list.push(entry);
  try {
    localStorage.setItem(ALERT_HISTORY_KEY, JSON.stringify(list.slice(-200)));
  } catch {
    // storage unavailable (private mode etc) — history just won't persist
  }
  renderAlertHistory();
}

function renderAlertHistory() {
  const container = document.querySelector("#alertHistoryList");
  if (!container) return;
  const cutoff = Date.now() - ALERT_HISTORY_WINDOW_MS;
  const list = loadAlertHistory()
    .filter((e) => e.ts >= cutoff)
    .sort((a, b) => b.ts - a.ts);

  if (!list.length) {
    container.innerHTML = `<p class="alert-history-empty">No notifications yet — you'll see device status changes here.</p>`;
    return;
  }

  container.innerHTML = list
    .map((e) => `
      <div class="alert-history-row">
        <span class="alert-history-dot ${e.online ? "online" : "offline"}"></span>
        <span class="alert-history-text">
          <span class="alert-history-device">${escapeHtml(e.name)}</span> is ${e.online ? "back online" : "offline"} · ${escapeHtml(e.ip || "")}
        </span>
        <span class="alert-history-time">${new Date(e.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
      </div>
    `)
    .join("");
}

function requestOnlineAlertPermission() {
  const statusEl = document.querySelector("#onlineAlertStatus");
  if (!window.Notification) {
    if (statusEl) statusEl.textContent = "Browser notifications aren't supported here — in-app toasts will still show.";
    return;
  }
  if (Notification.permission === "granted") {
    if (statusEl) statusEl.textContent = "Browser notifications are enabled.";
    return;
  }
  if (Notification.permission === "denied") {
    if (statusEl) statusEl.textContent = "Browser notifications are blocked — in-app toasts will still show.";
    return;
  }
  Notification.requestPermission().then((perm) => {
    if (statusEl) statusEl.textContent = perm === "granted"
      ? "Browser notifications are enabled."
      : "In-app toasts will show even without browser notifications.";
  });
}

function renderAll() {
  renderCompanySelect();
  renderDashboard();
  renderDevices();
  renderReports();
  renderCompanies();
  renderProfile();
  renderAuthPreviewBars();
}

// ── Auth preview bars ─────────────────────────────────────────────────────────
function renderAuthPreviewBars() {
  const container = document.querySelector("#authBars");
  if (!container) return;

  // Use current device health data for the auth preview
  const vals = [72, 85, 91, 78, 95, 88, 100, 82, 96, 73, 100, 94];
  container.innerHTML = vals.map((v) => {
    const clr = v >= 90 ? "#4ade80" : v >= 75 ? "#fbbf24" : "#f87171";
    return `<div class="preview-bar" style="height:${v}%;background:${clr};opacity:0.85;"></div>`;
  }).join("");

  // Update auth stats
  const online = statusDevices.filter((d) => d.confirmedStatus === "ONLINE" || d.status === "ONLINE").length;
  const offline = statusDevices.filter((d) => d.confirmedStatus === "OFFLINE" || d.status === "OFFLINE").length;
  const uptime = statusDevices.length ? `${Math.round((online / statusDevices.length) * 100)}%` : "—";

  const el = (id) => document.querySelector(id);
  if (el("#authOnline")) el("#authOnline").textContent = online || "—";
  if (el("#authOffline")) el("#authOffline").textContent = offline || "—";
  if (el("#authUptime")) el("#authUptime").textContent = uptime;
  if (el("#authIncidents")) el("#authIncidents").textContent = reports.length || "—";
}

// ── CSV helpers ───────────────────────────────────────────────────────────────
function downloadCsv(filename, rows) {
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

// ══════════════════════════════════════════════════════════════════════════════
//  FULLSCREEN OVERLAY
// ══════════════════════════════════════════════════════════════════════════════

function openDashboardOverlay() {
  if (!dashboardOverlay) return;
  renderOverlayDeviceList();
  renderOverlayStats();
  if (fsStatusRows) fsStatusRows.innerHTML = buildFsStatusRowsHTML();
  if (selectedDeviceId) loadOverlayChart();
  dashboardOverlay.classList.add("open");
  document.body.style.overflow = "hidden";
}

function closeDashboardOverlay() {
  if (!dashboardOverlay) return;
  dashboardOverlay.classList.remove("open");
  document.body.style.overflow = "";
}

// Build the full overlay device list (left column)
function renderOverlayDeviceList() {
  const scroll = document.querySelector("#overlayDeviceScroll");
  if (!scroll) return;

  const sorted = statusDevices.slice().sort((a, b) => statusSort(a.status) - statusSort(b.status));
  scroll.innerHTML = sorted.length
    ? sorted.map((d) => {
        const statusCls = badgeClass(d.confirmedStatus || d.status);
        const isActive = d.id === selectedDeviceId;
        return `
          <div class="overlay-device-item${isActive ? " active" : ""}" data-ov-device="${d.id}">
            <span class="overlay-device-dot ${statusCls}"></span>
            <div class="overlay-device-info">
              <div class="overlay-device-name">${escapeHtml(d.name)}</div>
              <div class="overlay-device-loc">${escapeHtml(d.location || "")}</div>
            </div>
            <span class="overlay-device-status ${statusCls}">${escapeHtml(d.confirmedStatus || d.status)}</span>
          </div>`;
      }).join("")
    : `<div style="padding:16px;font-size:12px;color:#475569;">No devices.</div>`;

  document.querySelectorAll("[data-ov-device]").forEach((item) => {
    item.addEventListener("click", () => {
      selectedDeviceId = item.dataset.ovDevice;
      // Update active state
      document.querySelectorAll("[data-ov-device]").forEach((el) => el.classList.remove("active"));
      item.classList.add("active");
      renderOverlayStats();
      if (fsStatusRows) fsStatusRows.innerHTML = buildFsStatusRowsHTML();
      loadOverlayChart();
    });
  });
}

// Build the live feed for the overlay mini table
function buildFsStatusRowsHTML() {
  const sorted = statusDevices.slice().sort((a, b) => statusSort(a.status) - statusSort(b.status));
  return sorted.length
    ? sorted.map((d) => `
      <tr>
        <td>${escapeHtml(d.location || "—")}</td>
        <td>${escapeHtml(d.name)}</td>
        <td style="font-family:'Roboto Mono',monospace;font-size:11px;">${escapeHtml(d.ip)}</td>
        <td><span class="badge ${badgeClass(d.confirmedStatus || d.status)}">${escapeHtml(d.confirmedStatus || d.status)}</span></td>
        <td style="font-family:'Roboto Mono',monospace;font-size:11px;">${escapeHtml(d.latency || d.ping || "—")}</td>
        <td>${escapeHtml(d.downCount || 0)}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" style="padding:12px;color:#475569;text-align:center;">No devices.</td></tr>`;
}

// Render the mini bars in the right stats panel (6 bars)
function renderOverlayMiniBars() {
  const container = document.querySelector("#overlayMiniBars");
  const labelEl = document.querySelector("#overlayMiniBarHealth");
  if (!container) return;

  const history = selectedDeviceId ? (deviceHistories[selectedDeviceId] || []) : [];
  const TOTAL = 6;
  const emptyBar = { time: "--:--", value: null, empty: true };
  const padded = Array(Math.max(0, TOTAL - history.length)).fill(null).map(() => ({ ...emptyBar }));
  const displayBars = [...padded, ...history];

  const currentPct = history.length ? history[history.length - 1].value : null;
  if (labelEl) {
    labelEl.textContent = currentPct !== null ? `${currentPct}%` : "—%";
    labelEl.style.color = currentPct !== null ? healthColorSelector(currentPct) : "#475569";
  }

  container.innerHTML = displayBars.map((bar, i) => {
    const h = bar.empty ? 0 : Math.max(bar.value, 4);
    const clr = bar.empty ? "rgba(255,255,255,0.06)" : healthColorSelector(bar.value);
    const isLive = i === TOTAL - 1 && !bar.empty;
    return `
      <div class="ombar-wrap" title="${bar.empty ? "No data" : `${bar.time} — ${bar.value}%`}">
        <div class="ombar-track">
          <div class="ombar${isLive ? " health-bar-live" : ""}" style="height:${h}%;background:${clr};border-radius:2px;transition:height 0.6s cubic-bezier(0.16,1,0.3,1);"></div>
        </div>
        <span class="ombar-time">${bar.empty ? "--" : bar.time.slice(0, 5)}</span>
      </div>`;
  }).join("");
}

// Render the right-panel stat cards
function renderOverlayStats() {
  const targetDevice = statusDevices.find((d) => d.id === selectedDeviceId);
  const infoEl = document.querySelector("#overlayDeviceInfoText");
  if (infoEl) {
    infoEl.innerHTML = targetDevice
      ? `<strong>${escapeHtml(targetDevice.name)}</strong><br><span style="color:var(--muted);">${escapeHtml(targetDevice.location || "")}</span><br><span style="font-family:'Roboto Mono',monospace;font-size:11px;color:var(--muted);">${escapeHtml(targetDevice.ip)}</span>`
      : "No device selected";
  }
}

// ── Overlay Chart (line/bar with colored points) ──────────────────────────────
async function loadOverlayChart() {
  if (!selectedDeviceId) return;
  const targetDevice = statusDevices.find((d) => d.id === selectedDeviceId);

  const titleEl = document.querySelector("#overlayChartTitle");
  const subEl = document.querySelector("#overlayChartSub");
  if (titleEl) titleEl.textContent = targetDevice ? targetDevice.name : "Device";
  
  let labelText = "Availability — ";
  if (ovActiveTimeframe === "today") labelText += "Today (24h)";
  else if (ovActiveTimeframe === "week") labelText += "Last 7 Days";
  else if (ovActiveTimeframe === "month") labelText += "Last 30 Days";
  else if (ovActiveTimeframe === "6months") labelText += "Last 6 Months";
  else if (ovActiveTimeframe === "custom") {
    const startVal = document.getElementById("overlayCustomStart")?.value || "";
    const endVal = document.getElementById("overlayCustomEnd")?.value || "";
    labelText += `Custom Range (${startVal} to ${endVal})`;
  }
  if (subEl) subEl.textContent = labelText;

  const periodEl = document.querySelector("#overlayPeriodLabel");
  if (periodEl) {
    if (ovActiveTimeframe === "today") periodEl.textContent = "Today";
    else if (ovActiveTimeframe === "week") periodEl.textContent = "Last Week";
    else if (ovActiveTimeframe === "month") periodEl.textContent = "Last Month";
    else if (ovActiveTimeframe === "6months") periodEl.textContent = "6 Months";
    else if (ovActiveTimeframe === "custom") periodEl.textContent = "Custom Range";
  }

  try {
    const params = new URLSearchParams({ timeframe: ovActiveTimeframe });
    if (ovActiveTimeframe === "custom") {
      const startVal = document.getElementById("overlayCustomStart")?.value || "";
      const endVal = document.getElementById("overlayCustomEnd")?.value || "";
      params.append("start", startVal);
      params.append("end", endVal);
    }
    const data = await api(`/api/devices/${selectedDeviceId}/analytics?${params.toString()}`);

    const avgHealth = data.overallAvg ?? data.avgHealth;
    const healthBigEl = document.querySelector("#overlayHealthBig");
    if (healthBigEl) {
      healthBigEl.textContent = `${avgHealth}%`;
      healthBigEl.style.color = healthColorSelector(avgHealth);
    }
    const avgEl = document.querySelector("#overlayAvgHealth");
    if (avgEl) {
      avgEl.textContent = `${avgHealth}%`;
      avgEl.style.color = healthColorSelector(avgHealth);
      avgEl.parentElement.className = `overlay-stat-card ${avgHealth >= 90 ? "good" : avgHealth >= 75 ? "warn" : "danger"}`;
    }

    renderOverlayChart(data.labels, data.uptime, data.chartType, avgHealth, data.todayHourly);
  } catch (err) {
    console.error("Overlay chart error:", err);
  }
}

function renderOverlayChart(labels, uptimeData, chartType, overallAvg, todayHourly) {
  const canvas = document.getElementById("overlayHistoricalChart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");

  if (overlayChartInstance) {
    overlayChartInstance.destroy();
    overlayChartInstance = null;
  }

  // Color helpers
  const pointColor = (v) => {
    if (v === null) return "rgba(200,200,200,0.4)";
    if (v >= 90) return "#4ade80";
    if (v >= 75) return "#fbbf24";
    return "#f87171";
  };

  const areaGradient = (ctx2, customHeight = 250) => {
    const gradient = ctx2.createLinearGradient(0, 0, 0, customHeight);
    gradient.addColorStop(0, "rgba(74,222,128,0.25)");
    gradient.addColorStop(0.5, "rgba(251,191,36,0.1)");
    gradient.addColorStop(1, "rgba(248,113,113,0.04)");
    return gradient;
  };

  const lightGridColor = "rgba(0, 0, 0, 0.05)";
  const lightTickColor = "#475569";
  const lightTitleColor = "#64748b";

  const commonScales = {
    y: {
      min: 0, max: 100,
      grid: { color: lightGridColor },
      ticks: { color: lightTickColor, font: { size: 10 }, callback: (v) => `${v}%` },
      title: { display: true, text: "Availability %", color: lightTitleColor, font: { size: 10 } },
    },
    x: {
      grid: { color: lightGridColor },
      ticks: { color: lightTickColor, font: { size: 9 }, maxRotation: 40, autoSkip: true, maxTicksLimit: 12 },
    },
  };

  if (chartType === "bar") {
    const barColors = uptimeData.map((v) => {
      if (v === null) return "rgba(148, 163, 184, 0.15)";
      if (v >= 90) return "#16a34a"; // Solid vibrant green
      if (v >= 75) return "#d97706"; // Solid vibrant orange
      return "#dc2626"; // Solid vibrant red
    });

    overlayChartInstance = new Chart(ctx, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "Availability %",
          data: uptimeData,
          backgroundColor: barColors,
          borderColor: barColors,
          borderWidth: 0,
          borderRadius: 4,
          barPercentage: 0.9,
          categoryPercentage: 0.9
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: commonScales,
        animation: {
          duration: 800,
          easing: "easeOutCubic",
          delay: (ctx2) => ctx2.type === "data" ? ctx2.dataIndex * 35 : 0,
        },
        animations: {
          y: { duration: 900, easing: "easeOutBack" },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(15,23,42,0.96)",
            titleColor: "#94a3b8",
            bodyColor: "#e2e8f0",
            callbacks: {
              label: (c) => c.parsed.y !== null ? `Availability: ${c.parsed.y}%` : "No data",
            },
          },
        },
      },
      plugins: [datalabelsPlugin]
    });
  } else {
    // Line chart with colored points + area gradient
    const pointColors = uptimeData.map(pointColor);

    overlayChartInstance = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "Availability %",
            data: uptimeData,
            borderColor: "#3b82f6",
            backgroundColor: areaGradient(ctx),
            borderWidth: 2.5,
            pointRadius: 5,
            pointHoverRadius: 8,
            pointBackgroundColor: pointColors,
            pointBorderColor: pointColors,
            pointBorderWidth: 2,
            fill: true,
            tension: 0.38,
            spanGaps: true,
          },
          {
            label: `Avg: ${overallAvg}%`,
            data: uptimeData.map(() => overallAvg),
            borderColor: "rgba(156,163,175,0.45)",
            borderWidth: 1.5,
            borderDash: [6, 4],
            pointRadius: 0,
            fill: false,
            tension: 0,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: commonScales,
        animation: {
          duration: 950,
          easing: "easeOutQuart",
        },
        animations: {
          radius: { duration: 400, easing: "easeOutElastic", from: 0 },
        },
        plugins: {
          legend: {
            display: true,
            position: "top",
            labels: {
              color: "#64748b",
              boxWidth: 12,
              font: { size: 10 },
              filter: (item) => item.datasetIndex === 1,
            },
          },
          tooltip: {
            backgroundColor: "rgba(15,23,42,0.96)",
            titleColor: "#94a3b8",
            bodyColor: "#e2e8f0",
            callbacks: {
              label: (c) => {
                if (c.datasetIndex === 1) return `Period avg: ${overallAvg}%`;
                return c.parsed.y !== null ? `Availability: ${c.parsed.y}%` : "No data";
              },
            },
          },
        },
      },
    });
  }
}

// ── Overlay timeframe buttons ──────────────────────────────────────────────────
document.querySelectorAll("[data-ov-tf]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("[data-ov-tf]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    ovActiveTimeframe = btn.dataset.ovTf;

    const periodEl = document.querySelector("#overlayPeriodLabel");
    if (periodEl) {
      if (ovActiveTimeframe === "today") periodEl.textContent = "Today";
      else if (ovActiveTimeframe === "week") periodEl.textContent = "Last Week";
      else if (ovActiveTimeframe === "month") periodEl.textContent = "Last Month";
      else if (ovActiveTimeframe === "6months") periodEl.textContent = "6 Months";
      else if (ovActiveTimeframe === "custom") periodEl.textContent = "Custom Range";
    }

    const customContainer = document.getElementById("overlayCustomRangeContainer");
    if (customContainer) {
      if (ovActiveTimeframe === "custom") {
        customContainer.classList.remove("hidden");
        // Initialize date values if empty
        const startInput = document.getElementById("overlayCustomStart");
        const endInput = document.getElementById("overlayCustomEnd");
        if (startInput && !startInput.value) {
          const today = new Date();
          const sevenDaysAgo = new Date();
          sevenDaysAgo.setDate(today.getDate() - 7);
          startInput.value = sevenDaysAgo.toISOString().split("T")[0];
          endInput.value = today.toISOString().split("T")[0];
        }
      } else {
        customContainer.classList.add("hidden");
      }
    }

    // Only load automatically if it's NOT custom; for custom range, wait for Apply Range click
    if (ovActiveTimeframe !== "custom" && selectedDeviceId) {
      loadOverlayChart();
    }
  });
});

// ── Overlay Apply Custom Range Button ──────────────────────────────────────────
const overlayApplyCustomBtn = document.getElementById("overlayApplyCustomBtn");
if (overlayApplyCustomBtn) {
  overlayApplyCustomBtn.addEventListener("click", () => {
    if (selectedDeviceId) {
      loadOverlayChart();
    }
  });
}

// Simple canvas text wrapper — breaks `text` onto multiple lines no wider
// than `maxWidth`, starting at (x, y) with `lineHeight` px between lines.
function wrapText(ctx, text, x, y, maxWidth, lineHeight) {
  const words = String(text).split(" ");
  let line = "";
  let curY = y;
  words.forEach((word) => {
    const testLine = line ? `${line} ${word}` : word;
    if (ctx.measureText(testLine).width > maxWidth && line) {
      ctx.fillText(line, x, curY);
      line = word;
      curY += lineHeight;
    } else {
      line = testLine;
    }
  });
  if (line) ctx.fillText(line, x, curY);
}

// ── Overlay download PNG ──────────────────────────────────────────────────────
const overlayDownloadChartBtn = document.querySelector("#overlayDownloadChartBtn");
if (overlayDownloadChartBtn) {
  overlayDownloadChartBtn.addEventListener("click", () => {
    const canvas1 = document.getElementById("overlayHistoricalChart");
    if (!canvas1 || !overlayChartInstance) return;

    const device = statusDevices.find((d) => d.id === selectedDeviceId);
    const deviceName = device ? device.name.replace(/[^a-zA-Z0-9]/g, "_") : "device";

    const tfDisplayName = ovActiveTimeframe === "today" ? "Today" : ovActiveTimeframe === "week" ? "Last Week" : ovActiveTimeframe === "month" ? "Last Month" : ovActiveTimeframe === "6months" ? "6 Months" : `Custom (${document.getElementById("overlayCustomStart")?.value || ""} to ${document.getElementById("overlayCustomEnd")?.value || ""})`;

    // ── Layout: graph on the left, "Uptime Tools" name centered up top,
    // device details (location / name / IP) in a panel on the right ──
    const padding    = 24;
    const sidePanelW = 260;
    const headerH    = 92;

    const tmpCanvas = document.createElement("canvas");
    tmpCanvas.width  = padding + canvas1.width + padding + sidePanelW + padding;
    tmpCanvas.height = headerH + canvas1.height + padding;

    const tmpCtx = tmpCanvas.getContext("2d");
    tmpCtx.fillStyle = "#ffffff";
    tmpCtx.fillRect(0, 0, tmpCanvas.width, tmpCanvas.height);

    // Centered header: app name + report subtitle
    tmpCtx.textAlign = "center";
    tmpCtx.fillStyle = "#182033";
    tmpCtx.font = "bold 20px 'Outfit', sans-serif";
    tmpCtx.fillText("Uptime Tools", tmpCanvas.width / 2, 38);
    tmpCtx.font = "500 13px 'Outfit', sans-serif";
    tmpCtx.fillStyle = "#697386";
    tmpCtx.fillText(`Graph Report — ${tfDisplayName}`, tmpCanvas.width / 2, 60);
    tmpCtx.fillText(`Generated: ${new Date().toLocaleString()}`, tmpCanvas.width / 2, 78);
    tmpCtx.textAlign = "left";

    // Left: the graph
    const chartX = padding;
    const chartY = headerH;
    tmpCtx.drawImage(canvas1, chartX, chartY);

    // Right: device details panel, vertically aligned with the chart
    const panelX = chartX + canvas1.width + padding;
    const panelW = sidePanelW - padding;
    const panelY = chartY;
    const panelH = canvas1.height;

    tmpCtx.fillStyle = "#f4f6fb";
    tmpCtx.strokeStyle = "#e2e6f0";
    tmpCtx.lineWidth = 1;
    const r = 10;
    tmpCtx.beginPath();
    tmpCtx.moveTo(panelX + r, panelY);
    tmpCtx.arcTo(panelX + panelW, panelY, panelX + panelW, panelY + panelH, r);
    tmpCtx.arcTo(panelX + panelW, panelY + panelH, panelX, panelY + panelH, r);
    tmpCtx.arcTo(panelX, panelY + panelH, panelX, panelY, r);
    tmpCtx.arcTo(panelX, panelY, panelX + panelW, panelY, r);
    tmpCtx.closePath();
    tmpCtx.fill();
    tmpCtx.stroke();

    const details = [
      { label: "DEVICE NAME", value: device ? device.name : "Unknown" },
      { label: "LOCATION",    value: device ? (device.location || "—") : "—" },
      { label: "IP ADDRESS",  value: device ? device.ip : "Unknown" },
    ];
    const rowGap = 64;
    const startY = panelY + (panelH - rowGap * (details.length - 1)) / 2 - 10;
    const textX = panelX + 20;
    details.forEach((d, i) => {
      const y = startY + i * rowGap;
      tmpCtx.font = "600 10px 'Outfit', sans-serif";
      tmpCtx.fillStyle = "#8791a8";
      tmpCtx.fillText(d.label, textX, y);
      tmpCtx.font = "600 15px 'Outfit', sans-serif";
      tmpCtx.fillStyle = "#182033";
      // Wrap long values so they don't spill outside the panel
      wrapText(tmpCtx, d.value, textX, y + 20, panelW - 40, 18);
    });

    const link = document.createElement("a");
    link.href = tmpCanvas.toDataURL("image/png");
    
    // Map internal timeframe code to clean filename labels
    let tfLabel = "Uptime";
    if (ovActiveTimeframe === "today") tfLabel = "Today";
    else if (ovActiveTimeframe === "week") tfLabel = "Last_Week";
    else if (ovActiveTimeframe === "month") tfLabel = "Last_Month";
    else if (ovActiveTimeframe === "6months") tfLabel = "Last_6_Months";
    else if (ovActiveTimeframe === "custom") {
      const s = document.getElementById("overlayCustomStart")?.value || "";
      const e = document.getElementById("overlayCustomEnd")?.value || "";
      tfLabel = s && e ? `Custom_${s}_to_${e}` : "Custom_Range";
    }

    link.download = `UptimeTools_${deviceName}_Uptime_${tfLabel}.png`;
    link.click();
  });
}

// ── Overlay Excel download ────────────────────────────────────────────────────
const overlayDownloadReportBtn = document.querySelector("#overlayDownloadReportBtn");
if (overlayDownloadReportBtn) {
  overlayDownloadReportBtn.addEventListener("click", () => {
    if (!activeCompanyId) return;
    const params = new URLSearchParams({
      timeframe: ovActiveTimeframe,
      device_id: selectedDeviceId || "",
    });
    if (ovActiveTimeframe === "custom") {
      const startVal = document.getElementById("overlayCustomStart")?.value || "";
      const endVal = document.getElementById("overlayCustomEnd")?.value || "";
      params.append("start", startVal);
      params.append("end", endVal);
    }
    window.location.href = `${API_BASE}/api/companies/${activeCompanyId}/analytics/export?${params.toString()}`;
  });
}

// ── Overlay open/close ────────────────────────────────────────────────────────
const expandDashboardBtn = document.querySelector("#expandDashboardBtn");
if (expandDashboardBtn) expandDashboardBtn.addEventListener("click", openDashboardOverlay);

const closeOverlayBtn = document.querySelector("#closeOverlayBtn");
if (closeOverlayBtn) closeOverlayBtn.addEventListener("click", closeDashboardOverlay);

if (dashboardOverlay) {
  dashboardOverlay.addEventListener("click", (e) => {
    if (e.target === dashboardOverlay) closeDashboardOverlay();
  });
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && dashboardOverlay && dashboardOverlay.classList.contains("open")) {
    closeDashboardOverlay();
  }
});

// ══════════════════════════════════════════════════════════════════════════════
//  INCIDENT LOG DEVICE DROPDOWN
// ══════════════════════════════════════════════════════════════════════════════
let deviceDropdownOpen = false;

function openDeviceDropdown() {
  if (!deviceDropdownList || !deviceFilterToggle) return;
  deviceDropdownOpen = true;
  deviceDropdownList.classList.add("open");
  deviceFilterToggle.classList.add("open");
  deviceFilterToggle.setAttribute("aria-expanded", "true");
}

function closeDeviceDropdown() {
  if (!deviceDropdownList || !deviceFilterToggle) return;
  deviceDropdownOpen = false;
  deviceDropdownList.classList.remove("open");
  deviceFilterToggle.classList.remove("open");
  deviceFilterToggle.setAttribute("aria-expanded", "false");
}

if (deviceFilterToggle) {
  deviceFilterToggle.addEventListener("click", (e) => {
    e.stopPropagation();
    deviceDropdownOpen ? closeDeviceDropdown() : openDeviceDropdown();
  });
}

if (incidentStartDate) {
  incidentStartDate.addEventListener("change", () => {
    reportStartDate = incidentStartDate.value;
    if (reportEndDate && reportStartDate && reportEndDate < reportStartDate) {
      reportEndDate = reportStartDate;
      incidentEndDate.value = reportEndDate;
    }
    refreshReports();
  });
}
if (incidentEndDate) {
  incidentEndDate.addEventListener("change", () => {
    reportEndDate = incidentEndDate.value;
    if (reportStartDate && reportEndDate && reportEndDate < reportStartDate) {
      reportStartDate = reportEndDate;
      incidentStartDate.value = reportStartDate;
    }
    refreshReports();
  });
}
if (incidentDateClearBtn) {
  incidentDateClearBtn.addEventListener("click", () => {
    reportStartDate = "";
    reportEndDate = "";
    if (incidentStartDate) incidentStartDate.value = "";
    if (incidentEndDate) incidentEndDate.value = "";
    refreshReports();
  });
}

document.addEventListener("click", (e) => {
  if (deviceDropdownOpen && deviceFilterToggle && !deviceFilterToggle.closest(".device-dropdown-wrap").contains(e.target)) {
    closeDeviceDropdown();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && deviceDropdownOpen) closeDeviceDropdown();
});

// ══════════════════════════════════════════════════════════════════════════════
//  AUTH FORMS
// ══════════════════════════════════════════════════════════════════════════════
document.querySelectorAll(".password-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.toggleTarget);
    if (!input) return;
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    btn.setAttribute("aria-pressed", String(!showing));
    btn.setAttribute("aria-label", showing ? "Show password" : "Hide password");
  });
});

document.querySelectorAll(".auth-tab").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".auth-tab").forEach((t) => t.classList.remove("active"));
  btn.classList.add("active");
  const isLogin = btn.dataset.authTab === "login";
  document.querySelector("#loginForm").classList.toggle("hidden", !isLogin);
  document.querySelector("#signupForm").classList.toggle("hidden", isLogin);
}));

document.querySelector("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  authMessage.textContent = "";
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn?.classList.add("is-loading");
  showLoadingOverlay("Signing you in…");
  try {
    const data = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ identifier: loginIdentifier.value, password: loginPassword.value }),
    });
    currentUser = data.user;
    showApp();
  } catch (err) {
    authMessage.textContent = err.message;
  } finally {
    submitBtn?.classList.remove("is-loading");
    hideLoadingOverlay();
  }
});

// Live username availability check (debounced) + suggest button
let usernameCheckTimer = null;
const signupUsernameInput = document.querySelector("#signupUsername");
const usernameHint = document.querySelector("#usernameHint");
if (signupUsernameInput) {
  signupUsernameInput.addEventListener("input", () => {
    clearTimeout(usernameCheckTimer);
    const val = signupUsernameInput.value.trim().toLowerCase();
    if (!val) {
      if (usernameHint) { usernameHint.textContent = "This is your unique ID — use it to log in instead of your email."; usernameHint.className = "username-hint"; }
      return;
    }
    usernameCheckTimer = setTimeout(async () => {
      try {
        const res = await api("/api/username-check", { method: "POST", body: JSON.stringify({ username: val }) });
        if (usernameHint) {
          usernameHint.textContent = res.available ? "Available." : "That user ID is already taken.";
          usernameHint.className = `username-hint ${res.available ? "available" : "taken"}`;
        }
      } catch (e2) { /* ignore */ }
    }, 350);
  });
}
const suggestUsernameBtn = document.querySelector("#suggestUsernameBtn");
if (suggestUsernameBtn) {
  suggestUsernameBtn.addEventListener("click", async () => {
    try {
      const res = await api("/api/username-suggest", {
        method: "POST",
        body: JSON.stringify({ name: signupName.value, email: signupEmail.value }),
      });
      signupUsernameInput.value = res.username;
      if (usernameHint) { usernameHint.textContent = "Available."; usernameHint.className = "username-hint available"; }
    } catch (e2) { /* ignore */ }
  });
}

document.querySelector("#signupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  authMessage.textContent = "";
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn?.classList.add("is-loading");
  showLoadingOverlay("Creating your account…");
  try {
    const data = await api("/api/signup", {
      method: "POST",
      body: JSON.stringify({
        name: signupName.value,
        email: signupEmail.value,
        username: signupUsernameInput ? signupUsernameInput.value : "",
        password: signupPassword.value,
        company: signupCompany.value,
      }),
    });
    currentUser = data.user;
    showApp();
  } catch (err) {
    authMessage.textContent = err.message;
  } finally {
    submitBtn?.classList.remove("is-loading");
    hideLoadingOverlay();
  }
});

// ══════════════════════════════════════════════════════════════════════════════
//  FORGOT PASSWORD
// ══════════════════════════════════════════════════════════════════════════════
const authTabsEl = document.querySelector(".auth-tabs");
const loginFormEl = document.querySelector("#loginForm");
const signupFormEl = document.querySelector("#signupForm");
const forgotEmailForm = document.querySelector("#forgotEmailForm");
const forgotResetForm = document.querySelector("#forgotResetForm");
const forgotEmailInput = document.querySelector("#forgotEmail");
const forgotResetHint = document.querySelector("#forgotResetHint");
let pendingResetEmail = "";

function showAuthForm(which) {
  // which: "login" | "signup" | "forgotEmail" | "forgotReset"
  authMessage.textContent = "";
  authTabsEl?.classList.toggle("hidden", which === "forgotEmail" || which === "forgotReset");
  loginFormEl?.classList.toggle("hidden", which !== "login");
  signupFormEl?.classList.toggle("hidden", which !== "signup");
  forgotEmailForm?.classList.toggle("hidden", which !== "forgotEmail");
  forgotResetForm?.classList.toggle("hidden", which !== "forgotReset");
  if (which === "login") {
    document.querySelectorAll(".auth-tab").forEach((t) => t.classList.toggle("active", t.dataset.authTab === "login"));
  }
}

document.querySelector("#showForgotPasswordBtn")?.addEventListener("click", () => {
  forgotEmailInput.value = loginIdentifier.value.includes("@") ? loginIdentifier.value : "";
  showAuthForm("forgotEmail");
});
document.querySelector("#backToLoginFromEmailBtn")?.addEventListener("click", () => showAuthForm("login"));
document.querySelector("#backToLoginFromResetBtn")?.addEventListener("click", () => showAuthForm("login"));

forgotEmailForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  authMessage.textContent = "";
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn?.classList.add("is-loading");
  showLoadingOverlay("Sending reset code…");
  try {
    const email = forgotEmailInput.value.trim();
    await api("/api/forgot-password", { method: "POST", body: JSON.stringify({ email }) });
    pendingResetEmail = email;
    if (forgotResetHint) forgotResetHint.textContent = `Enter the 6-digit code sent to ${email}.`;
    showAuthForm("forgotReset");
  } catch (err) {
    authMessage.textContent = err.message;
  } finally {
    submitBtn?.classList.remove("is-loading");
    hideLoadingOverlay();
  }
});

forgotResetForm?.addEventListener("submit", async (e) => {
  e.preventDefault();
  authMessage.textContent = "";
  const submitBtn = e.target.querySelector('button[type="submit"]');
  submitBtn?.classList.add("is-loading");
  showLoadingOverlay("Resetting your password…");
  try {
    await api("/api/reset-password", {
      method: "POST",
      body: JSON.stringify({
        email: pendingResetEmail,
        code: document.querySelector("#forgotCode").value.trim(),
        new_password: document.querySelector("#forgotNewPassword").value,
      }),
    });
    showAuthForm("login");
    loginIdentifier.value = pendingResetEmail;
    authMessage.style.color = "var(--online, #16a34a)";
    authMessage.textContent = "Password updated. Please log in.";
  } catch (err) {
    authMessage.style.color = "";
    authMessage.textContent = err.message;
  } finally {
    submitBtn?.classList.remove("is-loading");
    hideLoadingOverlay();
  }
});

// ══════════════════════════════════════════════════════════════════════════════
//  NAV
// ══════════════════════════════════════════════════════════════════════════════
document.querySelectorAll(".nav-item").forEach((btn) => btn.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  btn.classList.add("active");
  document.querySelector(`#${btn.dataset.page}`).classList.add("active");
  pageTitle.textContent = btn.querySelector(".nav-label")?.textContent.trim() || btn.textContent.trim();
  if (btn.dataset.page === "dashboard") replayLogoAnimation();
  if (btn.dataset.page === "profile") renderProfile();
  closeMobileNav();
}));

const topbarProfileBtn = document.querySelector("#topbarProfileBtn");
if (topbarProfileBtn) topbarProfileBtn.addEventListener("click", () => {
  document.querySelector('.nav-item[data-page="profile"]')?.click();
});

// ══════════════════════════════════════════════════════════════════════════════
//  MOBILE NAV DRAWER
// ══════════════════════════════════════════════════════════════════════════════
const mobileNavToggle = document.querySelector("#mobileNavToggle");
const mobileNavScrim = document.querySelector("#mobileNavScrim");
function openMobileNav() {
  appView.classList.add("nav-open");
  mobileNavToggle?.setAttribute("aria-expanded", "true");
}
function closeMobileNav() {
  appView.classList.remove("nav-open");
  mobileNavToggle?.setAttribute("aria-expanded", "false");
}
if (mobileNavToggle) mobileNavToggle.addEventListener("click", () => {
  appView.classList.contains("nav-open") ? closeMobileNav() : openMobileNav();
});
if (mobileNavScrim) mobileNavScrim.addEventListener("click", closeMobileNav);

// ══════════════════════════════════════════════════════════════════════════════
//  MISC EVENT BINDINGS
// ══════════════════════════════════════════════════════════════════════════════
if (companySelect) companySelect.addEventListener("change", async () => { activeCompanyId = companySelect.value; activeReportIp = "all"; selectedDeviceId = null; notificationDirty = false; await loadAll(); });
if (window.refreshBtn) refreshBtn.addEventListener("click", loadAll);
if (window.logoutBtn) logoutBtn.addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); showAuth(); });

if (window.showAddDeviceBtn) showAddDeviceBtn.addEventListener("click", () => addDeviceForm.classList.remove("hidden"));
if (window.cancelAddDeviceBtn) cancelAddDeviceBtn.addEventListener("click", () => { addDeviceForm.reset(); addDeviceForm.classList.add("hidden"); });
if (window.addDeviceForm) addDeviceForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await api(`/api/companies/${activeCompanyId}/devices`, { method: "POST", body: JSON.stringify({ location: newLocation.value, name: newDeviceName.value, ip: newIpAddress.value }) });
  addDeviceForm.reset();
  addDeviceForm.classList.add("hidden");
  await loadAll();
});

if (window.showImportCsvBtn) showImportCsvBtn.addEventListener("click", () => csvImportPanel.classList.toggle("hidden"));
if (window.importCsvBtn) importCsvBtn.addEventListener("click", async () => {
  const file = csvFileInput.files[0];
  if (!file) return alert("Choose a CSV file first.");
  if (file.size === 0) return alert("That file looks empty — please choose a CSV with at least a header row and one device.");
  try {
    const result = await withProgressToast(`Importing ${file.name}`, async () => {
      const csv = await file.text();
      return api(`/api/companies/${activeCompanyId}/devices/import`, { method: "POST", body: JSON.stringify({ csv }) });
    });
    csvFileInput.value = "";
    csvImportPanel.classList.add("hidden");
    if (result && result.skipped) {
      showToast({
        title: `Imported ${result.imported} device${result.imported === 1 ? "" : "s"}`,
        sub: `${result.skipped} row${result.skipped === 1 ? "" : "s"} skipped (missing IP or blank)`,
        type: "success",
      });
      setTimeout(() => {
        document.querySelectorAll(".toast").forEach((t) => {
          if (t.querySelector(".toast-title")?.textContent?.startsWith("Imported")) {
            t.classList.add("toast-out");
            setTimeout(() => t.remove(), 220);
          }
        });
      }, 5000);
    }
    await loadAll();
  } catch (err) {
    // withProgressToast already surfaced the error toast
  }
});

if (window.exportDevicesBtn) exportDevicesBtn.addEventListener("click", () => {
  withProgressToast("Exporting devices.csv", async () => {
    const rows = ["Location,Name,IP", ...devices.map((d) => `${d.location},${d.name},${d.ip}`)];
    downloadCsv("devices.csv", rows);
  });
});

if (window.downloadReportBtn) downloadReportBtn.addEventListener("click", () => {
  withProgressToast("Exporting outage_report.csv", async () => {
    const selected = activeReportIp === "all" ? reports : reports.filter((r) => r.ip === activeReportIp);
    const rows = ["Date,Location,Device Name,IP Address,Offline Time,Online Time,Downtime", ...selected.map((r) => `${r.date},${r.location},${r.name},${r.ip},${r.offline},${r.online},${r.downtime}`)];
    downloadCsv("outage_report.csv", rows);
  });
});

if (window.showAddCompanyBtn) showAddCompanyBtn.addEventListener("click", () => addCompanyForm.classList.remove("hidden"));
if (window.cancelAddCompanyBtn) cancelAddCompanyBtn.addEventListener("click", () => { addCompanyForm.reset(); addCompanyForm.classList.add("hidden"); });
if (window.addCompanyForm) addCompanyForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = await api("/api/companies", { method: "POST", body: JSON.stringify({ name: newCompanyName.value, email: newCompanyEmail.value, plan: "Starter" }) });
  activeCompanyId = data.company.id;
  selectedDeviceId = null;
  addCompanyForm.reset();
  addCompanyForm.classList.add("hidden");
  await loadAll();
});

if (window.saveNotificationBtn) saveNotificationBtn.addEventListener("click", async () => {
  const company = activeCompany();
  const primaryEmail = (document.querySelector("#companyEmail")?.value || "").trim();
  const secondaryReceivers = (document.querySelector("#companyReceivers")?.value || "").trim();
  const email = primaryEmail || secondaryReceivers;
  const onlineAlertsEnabled = !!document.querySelector("#onlineAlertsToggle")?.checked;
  await api(`/api/companies/${company.id}`, {
    method: "PUT",
    body: JSON.stringify({
      ...company,
      email: email,
      receivers: secondaryReceivers || email,
      alert_after_seconds: Number(alertSeconds.value) || 30,
      online_alerts: onlineAlertsEnabled,
    }),
  });
  notificationDirty = false;
  if (onlineAlertsEnabled) requestOnlineAlertPermission();
  showToast({ title: "Alert preferences saved", type: "success" });
  await loadAll();
});

const onlineAlertsToggleEl = document.querySelector("#onlineAlertsToggle");
if (onlineAlertsToggleEl) onlineAlertsToggleEl.addEventListener("change", () => {
  notificationDirty = true;
  if (onlineAlertsToggleEl.checked) requestOnlineAlertPermission();
});

// Track notification form dirty state
["#companyEmail", "#companyReceivers", "#alertSeconds"].forEach((sel) => {
  const el = document.querySelector(sel);
  if (el) el.addEventListener("input", () => { notificationDirty = true; });
});

// ── Boot ──────────────────────────────────────────────────────────────────────
async function boot() {
  // Animate auth preview bars while loading
  renderAuthPreviewBars();
  showLoadingOverlay("Connecting to Uptime Tools…");
  try {
    const me = await api("/api/me");
    if (me.authenticated) {
      currentUser = me.user;
      showApp();
    } else {
      showAuth();
    }
  } catch (e) {
    // Not logged in — reveal the auth page now that the check is done
    showAuth();
  } finally {
    hideLoadingOverlay();
  }
}

boot();