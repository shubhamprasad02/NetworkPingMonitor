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
const reportFilters = document.querySelector("#reportFilters");
const reportRows = document.querySelector("#reportRows");
const API_BASE = window.location.protocol === "file:" ? "http://127.0.0.1:8080" : "";

let currentUser = null;
let companies = [];
let activeCompanyId = null;
let devices = [];
let statusDevices = [];
let reports = [];
let deviceHistories = {}; 
let selectedDeviceId = null; 
let activeReportIp = "all";
let editingDeviceId = null;
let editingCompanyId = null;
let notificationDirty = false;
let refreshTimer = null;

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) showAuth();
    throw new Error(data.error || "Request failed");
  }
  return data;
}

function activeCompany() {
  return companies.find((c) => c.id === activeCompanyId);
}

async function loadAll() {
  try {
    const companyData = await api("/api/companies");
    companies = companyData.companies || [];
    if (!activeCompanyId && companies.length) activeCompanyId = companies[0].id;
    if (!companies.find((c) => c.id === activeCompanyId) && companies.length) activeCompanyId = companies[0].id;
    if (!activeCompanyId) return renderAll();
    
    const [deviceData, reportData, statusData] = await Promise.all([
      api(`/api/companies/${activeCompanyId}/devices`),
      api(`/api/companies/${activeCompanyId}/reports`),
      api(`/api/companies/${activeCompanyId}/status`),
    ]);
    devices = deviceData.devices || [];
    reports = reportData.reports || [];
    statusDevices = statusData.devices || [];
    
    if (!selectedDeviceId && statusDevices.length) selectedDeviceId = statusDevices[0].id;
    updateUptimeHistory();
    renderAll();
  } catch (err) {
    console.error("Data synchronization fault: ", err);
  }
}

function updateUptimeHistory() {
  statusDevices.forEach((device) => {
    const isOnline = device.confirmedStatus === "ONLINE" || device.status === "ONLINE";
    const currentPct = isOnline ? 100 : 0;
    
    if (!deviceHistories[device.id] || deviceHistories[device.id].length === 0) {
      deviceHistories[device.id] = [];
      const baseTime = new Date();
      for (let i = 9; i >= 0; i--) {
        const past = new Date(baseTime.getTime() - i * 5 * 60 * 1000);
        deviceHistories[device.id].push({ time: past.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }), value: currentPct });
      }
    } else {
      const now = new Date();
      const roundedMin = Math.floor(now.getMinutes() / 5) * 5;
      const nowLabel = `${now.getHours().toString().padStart(2, '0')}:${roundedMin.toString().padStart(2, '0')}`;
      const history = deviceHistories[device.id];
      if (history[history.length - 1].time === nowLabel) {
        history[history.length - 1].value = Math.round((history[history.length - 1].value + currentPct) / 2);
      } else {
        history.push({ time: nowLabel, value: currentPct });
        if (history.length > 10) history.shift();
      }
    }
  });
}

function showApp() {
  authView.classList.add("hidden");
  appView.classList.remove("hidden");
  userLabel.textContent = currentUser?.email || "Portal";
  loadAll();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshIfIdle, 5000);
}

function showAuth() {
  appView.classList.add("hidden");
  authView.classList.remove("hidden");
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
}

function refreshIfIdle() {
  if (editingDeviceId || editingCompanyId || notificationDirty) return;
  loadAll();
}

function healthColorSelector(val) {
  if (val >= 90) return "#10b981"; 
  if (val >= 75) return "#eab308"; 
  return "#ef4444"; 
}

function renderDashboard() {
  const online = statusDevices.filter((d) => d.confirmedStatus === "ONLINE" || d.status === "ONLINE").length;
  const offline = statusDevices.filter((d) => d.confirmedStatus === "OFFLINE" || d.status === "OFFLINE").length;
  
  document.querySelector("#totalDevices").textContent = statusDevices.length;
  document.querySelector("#onlineDevices").textContent = online;
  document.querySelector("#offlineDevices").textContent = offline;
  document.querySelector("#reportCount").textContent = reports.length;
  
  statusRows.innerHTML = statusDevices.length
    ? statusDevices.slice().sort((a, b) => statusSort(a.status) - statusSort(b.status)).map((device) => `
      <tr class="${selectedDeviceId === device.id ? 'selected-device-row' : ''}" data-row-device-id="${device.id}">
        <td>Public IP</td>
        <td>${escapeHtml(device.name)}</td>
        <td>${escapeHtml(device.ip)}</td>
        <td><span class="badge ${badgeClass(device.confirmedStatus || device.status)}">${escapeHtml(device.confirmedStatus || device.status)}</span></td>
        <td>${escapeHtml(device.ping || "--")}</td>
        <td>${escapeHtml(device.downCount || 0)}</td>
        <td>${escapeHtml(device.downFor || "-")}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="7" class="empty-cell">No active tracking devices.</td></tr>`;
    
  lastUpdated.textContent = `${activeCompany()?.name || "System"} updated ${new Date().toLocaleTimeString()}`;
  
  document.querySelectorAll("[data-row-device-id]").forEach((row) => {
    row.addEventListener("click", () => { selectedDeviceId = row.dataset.rowDeviceId; renderDashboard(); });
  });
  renderHealthBars();
  renderNotificationFields();
}

function renderHealthBars() {
  const targetDevice = statusDevices.find(d => d.id === selectedDeviceId);
  const history = selectedDeviceId ? (deviceHistories[selectedDeviceId] || []) : [];
  const currentPct = history.length ? history[history.length - 1].value : 0;
  if (document.querySelector("#chartDeviceTitle")) document.querySelector("#chartDeviceTitle").textContent = targetDevice ? `${targetDevice.name}` : 'Uptime History';

  const heroNum = document.querySelector("#currentHealth");
  if (heroNum) { heroNum.textContent = `${currentPct}%`; heroNum.style.color = healthColorSelector(currentPct); }

  const container = document.querySelector("#healthBars");
  if (!container) return;
  const displayBars = history.length ? history : Array(10).fill(null).map(() => ({ time: "--:--", value: 100 }));
  
  container.innerHTML = displayBars.map((bar) => `
    <div class="health-bar-wrap" style="flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%;" title="Time: ${bar.time} | Uptime: ${bar.value}%">
      <div style="width: 100%; height: 120px; background-color: #f1f5f9; border-radius: 4px; display: flex; align-items: flex-end; overflow: hidden;">
        <div style="width: 100%; height: ${Math.max(bar.value, 6)}%; background-color: ${healthColorSelector(bar.value)}; border-radius: 2px;"></div>
      </div>
      <span style="font-size: 10px; color: #64748b; margin-top: 6px; font-family: monospace;">${bar.time}</span>
    </div>
  `).join("");
}

function badgeClass(status) { return status === "ONLINE" ? "online" : (status === "OFFLINE" ? "offline" : "checking"); }
function statusSort(status) { return status === "OFFLINE" ? 0 : (status?.startsWith("CHECK") ? 1 : 2); }
function renderCompanySelect() { companySelect.innerHTML = companies.map((c) => `<option value="${c.id}" ${c.id === activeCompanyId ? "selected" : ""}>${escapeHtml(c.name)}</option>`).join(""); }

function renderDevices() {
  if (!devices.length) { deviceCards.innerHTML = `<div class="empty-card">No devices yet.</div>`; return; }
  deviceCards.innerHTML = devices.map((device) => {
    if (editingDeviceId === device.id) {
      return `<article class="device-card"><form class="edit-device-form" data-id="${device.id}"><label>Location<input name="location" value="${escapeHtml(device.location)}" required /></label><label>Name<input name="name" value="${escapeHtml(device.name)}" required /></label><label>IP<input name="ip" value="${escapeHtml(device.ip)}" required /></label><div class="device-actions"><button type="submit">Save</button><button type="button" data-cancel-device>Cancel</button></div></form></article>`;
    }
    const status = statusDevices.find((item) => item.id === device.id)?.status || "UNKNOWN";
    return `<article class="device-card"><strong>${escapeHtml(device.name)}</strong><p>${escapeHtml(device.location)}<br>${escapeHtml(device.ip)}</p><span class="badge ${badgeClass(status)}">${escapeHtml(status)}</span><div class="device-actions"><button data-edit-device="${device.id}">Edit</button><button class="danger" data-remove-device="${device.id}">Remove</button></div></article>`;
  }).join("");
  bindDeviceActions();
}

function bindDeviceActions() {
  document.querySelectorAll("[data-edit-device]").forEach((b) => b.addEventListener("click", () => { editingDeviceId = b.dataset.editDevice; renderDevices(); }));
  document.querySelectorAll("[data-cancel-device]").forEach((b) => b.addEventListener("click", () => { editingDeviceId = null; renderDevices(); }));
  document.querySelectorAll("[data-remove-device]").forEach((b) => b.addEventListener("click", async () => {
    const d = devices.find((item) => item.id === b.dataset.removeDevice);
    if (!confirm(`Remove ${d.name}?`)) return;
    await api(`/api/companies/${activeCompanyId}/devices/${d.id}`, { method: "DELETE" });
    if (selectedDeviceId === d.id) selectedDeviceId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-device-form").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    await api(`/api/companies/${activeCompanyId}/devices/${form.dataset.id}`, { method: "PUT", body: JSON.stringify({ location: fd.get("location"), name: fd.get("name"), ip: fd.get("ip") }) });
    editingDeviceId = null;
    await loadAll();
  }));
}

function renderReports() {
  renderReportFilters();
  const filtered = activeReportIp === "all" ? reports : reports.filter((r) => r.ip === activeReportIp);
  reportRows.innerHTML = filtered.length
    ? filtered.map((r) => `<tr><td>${escapeHtml(r.date)}</td><td>${escapeHtml(r.location)}</td><td><span class="device-tag">${escapeHtml(r.name)}</span></td><td>${escapeHtml(r.ip)}</td><td>${escapeHtml(r.offline)}</td><td>${escapeHtml(r.online)}</td><td>${escapeHtml(r.downtime)}</td></tr>`).join("")
    : `<tr><td colspan="7" class="empty-cell">No records.</td></tr>`;
}

function renderReportFilters() {
  const known = new Map();
  devices.forEach((d) => known.set(d.ip, d.name));
  reports.forEach((r) => known.set(r.ip, r.name));
  reportFilters.innerHTML = [`<button class="report-chip ${activeReportIp === "all" ? "active" : ""}" data-ip="all">All Devices <span>${reports.length}</span></button>`]
    .concat([...known.entries()].map(([ip, name]) => `<button class="report-chip ${activeReportIp === ip ? "active" : ""}" data-ip="${escapeHtml(ip)}">${escapeHtml(name)} <span>${reports.filter((r) => r.ip === ip).length}</span></button>`)).join("");
  document.querySelectorAll("[data-ip]").forEach((b) => b.addEventListener("click", () => { activeReportIp = b.dataset.ip; renderReports(); }));
}

function renderCompanies() {
  companyCards.innerHTML = companies.map((c) => {
    if (editingCompanyId === c.id) {
      return `<article class="company-card active"><form class="edit-company-form" data-id="${c.id}"><label>Company Name<input name="name" value="${escapeHtml(c.name)}" required /></label><label>Contact Email<input name="email" type="email" value="${escapeHtml(c.receivers)}" required /></label><div class="device-actions"><button type="submit">Save</button><button type="button" data-cancel-company>Cancel</button></div></form></article>`;
    }
    return `<article class="company-card ${c.id === activeCompanyId ? "active" : ""}"><div class="company-card-top"><div><strong>${escapeHtml(c.name)}</strong><p>${escapeHtml(c.receivers)}</p></div></div><div class="device-actions"><button data-open-company="${c.id}">Open</button><button data-edit-company="${c.id}">Edit</button><button class="danger" data-remove-company="${c.id}">Remove</button></div></article>`;
  }).join("");
  bindCompanyActions();
}

function bindCompanyActions() {
  document.querySelectorAll("[data-open-company]").forEach((b) => b.addEventListener("click", async () => { activeCompanyId = b.dataset.openCompany; activeReportIp = "all"; selectedDeviceId = null; await loadAll(); }));
  document.querySelectorAll("[data-edit-company]").forEach((b) => b.addEventListener("click", () => { editingCompanyId = b.dataset.editCompany; renderCompanies(); }));
  document.querySelectorAll("[data-cancel-company]").forEach((b) => b.addEventListener("click", () => { editingCompanyId = null; renderCompanies(); }));
  document.querySelectorAll("[data-remove-company]").forEach((b) => b.addEventListener("click", async () => {
    const c = companies.find((item) => item.id === b.dataset.removeCompany);
    if (!confirm(`Remove ${c.name}?`)) return;
    await api(`/api/companies/${c.id}`, { method: "DELETE" });
    if (activeCompanyId === c.id) activeCompanyId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-company-form").forEach((form) => form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    await api(`/api/companies/${form.dataset.id}`, { method: "PUT", body: JSON.stringify({ name: fd.get("name"), receivers: fd.get("email"), email: fd.get("email") }) });
    editingCompanyId = null;
    await loadAll();
  }));
}

function renderNotificationFields() {
  const company = activeCompany();
  if (!company || notificationDirty) return;
  if (document.querySelector("#companyReceivers")) document.querySelector("#companyReceivers").value = company.receivers || "";
  if (document.querySelector("#alertSeconds")) document.querySelector("#alertSeconds").value = company.alert_after_seconds || 30;
}

function renderAll() { renderCompanySelect(); renderDashboard(); renderDevices(); renderReports(); renderCompanies(); }

document.querySelectorAll(".auth-tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".auth-tab").forEach((t) => t.classList.remove("active"));
  b.classList.add("active");
  const login = b.dataset.authTab === "login";
  document.querySelector("#loginForm").classList.toggle("hidden", !login);
  document.querySelector("#signupForm").classList.toggle("hidden", login);
}));

document.querySelector("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/login", { method: "POST", body: JSON.stringify({ email: loginEmail.value, password: loginPassword.value }) });
    currentUser = data.user;
    showApp();
  } catch (error) { authMessage.textContent = error.message; }
});

document.querySelector("#signupForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/signup", { method: "POST", body: JSON.stringify({ name: signupName.value, email: signupEmail.value, password: signupPassword.value, company: signupCompany.value }) });
    currentUser = data.user;
    showApp();
  } catch (error) { authMessage.textContent = error.message; }
});

document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((i) => i.classList.remove("active"));
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  b.classList.add("active");
  document.querySelector(`#${b.dataset.page}`).classList.add("active");
  pageTitle.textContent = b.textContent;
}));

if (companySelect) companySelect.addEventListener("change", async () => { activeCompanyId = companySelect.value; activeReportIp = "all"; selectedDeviceId = null; notificationDirty = false; await loadAll(); });
if (window.refreshBtn) refreshBtn.addEventListener("click", loadAll);
if (window.logoutBtn) logoutBtn.addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); currentUser = null; showAuth(); });

if (window.addDeviceForm) addDeviceForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  await api(`/api/companies/${activeCompanyId}/devices`, { method: "POST", body: JSON.stringify({ location: newLocation.value, name: newDeviceName.value, ip: newIpAddress.value }) });
  addDeviceForm.reset();
  await loadAll();
});

if (window.saveNotificationBtn) saveNotificationBtn.addEventListener("click", async () => {
  const company = activeCompany();
  const targetEmail = companyReceivers.value;
  await api(`/api/companies/${company.id}`, { method: "PUT", body: JSON.stringify({ ...company, email: targetEmail, receivers: targetEmail, alert_after_seconds: Number(alertSeconds.value) || 30 }) });
  notificationDirty = false;
  alert("Settings saved successfully.");
  await loadAll();
});

if (window.companyReceivers && window.alertSeconds) {
  [companyReceivers, alertSeconds].forEach((f) => f.addEventListener("input", () => { notificationDirty = true; }));
}

async function boot() {
  try {
    const me = await api("/api/me");
    if (me.authenticated) { currentUser = me.user; showApp(); } else { showAuth(); }
  } catch (e) { showAuth(); }
}
boot();