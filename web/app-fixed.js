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

let currentUser = null;
let companies = [];
let activeCompanyId = null;
let devices = [];
let statusDevices = [];
let reports = [];
let uptimeHistory = [];
let activeReportIp = "all";
let editingDeviceId = null;
let editingCompanyId = null;
let notificationDirty = false;
let refreshTimer = null;

function escapeHtml(value) {
  return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function activeCompany() {
  return companies.find((company) => company.id === activeCompanyId);
}

async function loadAll() {
  const companyData = await api("/api/companies");
  companies = companyData.companies || [];
  if (!activeCompanyId && companies.length) activeCompanyId = companies[0].id;
  if (!companies.find((company) => company.id === activeCompanyId) && companies.length) activeCompanyId = companies[0].id;
  if (!activeCompanyId) return renderAll();
  const [deviceData, reportData, statusData] = await Promise.all([
    api(`/api/companies/${activeCompanyId}/devices`),
    api(`/api/companies/${activeCompanyId}/reports`),
    api(`/api/companies/${activeCompanyId}/status`),
  ]);
  devices = deviceData.devices || [];
  reports = reportData.reports || [];
  statusDevices = statusData.devices || [];
  updateUptimeHistory();
  renderAll();
}

function isInputActiveInside(selector) {
  const active = document.activeElement;
  return Boolean(active && active.closest && active.closest(selector));
}

function hasUnsavedEdits() {
  return Boolean(
    editingDeviceId ||
    editingCompanyId ||
    notificationDirty ||
    isInputActiveInside("#addDeviceForm") ||
    isInputActiveInside("#csvImportPanel") ||
    isInputActiveInside("#addCompanyForm")
  );
}

async function refreshIfIdle() {
  if (hasUnsavedEdits()) return;
  await loadAll();
}

function updateUptimeHistory() {
  if (!statusDevices.length) {
    uptimeHistory = [...uptimeHistory.slice(-11), 0];
    return;
  }
  const online = statusDevices.filter((device) => device.confirmedStatus === "ONLINE").length;
  uptimeHistory = [...uptimeHistory.slice(-11), Math.round((online / statusDevices.length) * 100)];
}

function showApp() {
  authView.classList.add("hidden");
  appView.classList.remove("hidden");
  userLabel.textContent = currentUser?.email || "Portal";
  loadAll();
  if (!refreshTimer) refreshTimer = setInterval(refreshIfIdle, 5000);
}

function showAuth() {
  appView.classList.add("hidden");
  authView.classList.remove("hidden");
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = null;
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

function renderCompanySelect() {
  companySelect.innerHTML = companies.map((company) => `<option value="${company.id}" ${company.id === activeCompanyId ? "selected" : ""}>${escapeHtml(company.name)}</option>`).join("");
}

function renderDashboard() {
  const online = statusDevices.filter((device) => device.confirmedStatus === "ONLINE").length;
  const offline = statusDevices.filter((device) => device.confirmedStatus === "OFFLINE").length;
  document.querySelector("#totalDevices").textContent = statusDevices.length;
  document.querySelector("#onlineDevices").textContent = online;
  document.querySelector("#offlineDevices").textContent = offline;
  document.querySelector("#reportCount").textContent = reports.length;
  statusRows.innerHTML = statusDevices.length
    ? statusDevices.slice().sort((a, b) => statusSort(a.status) - statusSort(b.status)).map((device) => `
      <tr><td>${escapeHtml(device.location)}</td><td>${escapeHtml(device.name)}</td><td>${escapeHtml(device.ip)}</td><td><span class="badge ${badgeClass(device.status)}">${escapeHtml(device.status)}</span></td><td>${escapeHtml(device.ping)}</td><td>${escapeHtml(device.downCount)}</td><td>${escapeHtml(device.downFor)}</td></tr>
    `).join("")
    : `<tr><td colspan="7" class="empty-cell">No devices in this company yet.</td></tr>`;
  lastUpdated.textContent = `${activeCompany()?.name || "No company"} updated ${new Date().toLocaleTimeString()}`;
  renderHealthBars();
  renderNotificationFields();
}

function renderDevices() {
  if (!devices.length) {
    deviceCards.innerHTML = `<div class="empty-card">No devices yet. Add one from the website or import a CSV file.</div>`;
    return;
  }
  deviceCards.innerHTML = devices.map((device) => {
    if (editingDeviceId === device.id) {
      return `<article class="device-card"><form class="edit-device-form" data-id="${device.id}"><label>Location<input name="location" value="${escapeHtml(device.location)}" required /></label><label>Device Name<input name="name" value="${escapeHtml(device.name)}" required /></label><label>IP Address<input name="ip" value="${escapeHtml(device.ip)}" required /></label><div class="device-actions"><button class="small-action" type="submit">Save</button><button class="small-action danger" type="button" data-cancel-device>Cancel</button></div></form></article>`;
    }
    const status = statusDevices.find((item) => item.id === device.id)?.status || "UNKNOWN";
    return `<article class="device-card"><strong>${escapeHtml(device.name)}</strong><p>${escapeHtml(device.location)}<br>${escapeHtml(device.ip)}</p><span class="badge ${badgeClass(status)}">${escapeHtml(status)}</span><div class="device-actions"><button class="small-action" data-edit-device="${device.id}">Edit</button><button class="small-action danger" data-remove-device="${device.id}">Remove</button></div></article>`;
  }).join("");
  bindDeviceActions();
}

function bindDeviceActions() {
  document.querySelectorAll("[data-edit-device]").forEach((button) => button.addEventListener("click", () => { editingDeviceId = button.dataset.editDevice; renderDevices(); }));
  document.querySelectorAll("[data-cancel-device]").forEach((button) => button.addEventListener("click", () => { editingDeviceId = null; renderDevices(); }));
  document.querySelectorAll("[data-remove-device]").forEach((button) => button.addEventListener("click", async () => {
    const device = devices.find((item) => item.id === button.dataset.removeDevice);
    if (!confirm(`Remove ${device.name} (${device.ip})?`)) return;
    await api(`/api/companies/${activeCompanyId}/devices/${device.id}`, { method: "DELETE" });
    editingDeviceId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-device-form").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    await api(`/api/companies/${activeCompanyId}/devices/${form.dataset.id}`, {
      method: "PUT",
      body: JSON.stringify({ location: formData.get("location"), name: formData.get("name"), ip: formData.get("ip") }),
    });
    editingDeviceId = null;
    await loadAll();
  }));
}

function renderReports() {
  renderReportFilters();
  const filtered = activeReportIp === "all" ? reports : reports.filter((report) => report.ip === activeReportIp);
  reportRows.innerHTML = filtered.length
    ? filtered.map((report) => `<tr><td>${escapeHtml(report.date)}</td><td>${escapeHtml(report.location)}</td><td><span class="device-tag">${escapeHtml(report.name)}</span></td><td>${escapeHtml(report.ip)}</td><td>${escapeHtml(report.offline)}</td><td>${escapeHtml(report.online)}</td><td>${escapeHtml(report.downtime)}</td></tr>`).join("")
    : `<tr><td colspan="7" class="empty-cell">No outage reports for this filter yet.</td></tr>`;
}

function renderReportFilters() {
  const known = new Map();
  devices.forEach((device) => known.set(device.ip, device.name));
  reports.forEach((report) => known.set(report.ip, report.name));
  reportFilters.innerHTML = [`<button class="report-chip ${activeReportIp === "all" ? "active" : ""}" data-ip="all">All Devices <span>${reports.length}</span></button>`]
    .concat([...known.entries()].map(([ip, name]) => `<button class="report-chip ${activeReportIp === ip ? "active" : ""}" data-ip="${escapeHtml(ip)}">${escapeHtml(name)} <span>${reports.filter((r) => r.ip === ip).length}</span></button>`))
    .join("");
  document.querySelectorAll("[data-ip]").forEach((button) => button.addEventListener("click", () => { activeReportIp = button.dataset.ip; renderReports(); }));
}

function renderCompanies() {
  companyCards.innerHTML = companies.map((company) => {
    if (editingCompanyId === company.id) {
      return `<article class="company-card active"><form class="edit-company-form" data-id="${company.id}"><label>Company Name<input name="name" value="${escapeHtml(company.name)}" required /></label><label>Contact Email<input name="email" type="email" value="${escapeHtml(company.email)}" required /></label><label>Plan<select name="plan"><option ${company.plan === "Starter" ? "selected" : ""}>Starter</option><option ${company.plan === "Business" ? "selected" : ""}>Business</option><option ${company.plan === "Pro" ? "selected" : ""}>Pro</option></select></label><div class="device-actions"><button class="small-action" type="submit">Save</button><button class="small-action danger" type="button" data-cancel-company>Cancel</button></div></form></article>`;
    }
    const count = devicesForCompany(company.id);
    return `<article class="company-card ${company.id === activeCompanyId ? "active" : ""}"><div class="company-card-top"><div><strong>${escapeHtml(company.name)}</strong><p>${escapeHtml(company.email)}</p></div><span class="plan-pill">${escapeHtml(company.plan)}</span></div><div class="company-mini-stats"><span>${count.devices} devices</span><span>${count.reports} reports</span></div><div class="device-actions"><button class="small-action" data-open-company="${company.id}">Open</button><button class="small-action" data-edit-company="${company.id}">Edit</button><button class="small-action danger" data-remove-company="${company.id}">Remove</button></div></article>`;
  }).join("");
  bindCompanyActions();
}

function devicesForCompany(companyId) {
  if (companyId === activeCompanyId) return { devices: devices.length, reports: reports.length };
  return { devices: "-", reports: "-" };
}

function bindCompanyActions() {
  document.querySelectorAll("[data-open-company]").forEach((button) => button.addEventListener("click", async () => { activeCompanyId = button.dataset.openCompany; activeReportIp = "all"; await loadAll(); }));
  document.querySelectorAll("[data-edit-company]").forEach((button) => button.addEventListener("click", () => { editingCompanyId = button.dataset.editCompany; renderCompanies(); }));
  document.querySelectorAll("[data-cancel-company]").forEach((button) => button.addEventListener("click", () => { editingCompanyId = null; renderCompanies(); }));
  document.querySelectorAll("[data-remove-company]").forEach((button) => button.addEventListener("click", async () => {
    const company = companies.find((item) => item.id === button.dataset.removeCompany);
    if (!confirm(`Remove ${company.name}? Devices and reports for this company will be removed.`)) return;
    await api(`/api/companies/${company.id}`, { method: "DELETE" });
    activeCompanyId = null;
    await loadAll();
  }));
  document.querySelectorAll(".edit-company-form").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const formData = new FormData(form);
    await api(`/api/companies/${form.dataset.id}`, { method: "PUT", body: JSON.stringify({ name: formData.get("name"), email: formData.get("email"), plan: formData.get("plan") }) });
    editingCompanyId = null;
    await loadAll();
  }));
}

function renderNotificationFields() {
  const company = activeCompany();
  if (!company || notificationDirty) return;
  document.querySelector("#companyEmail").value = company.email || "";
  document.querySelector("#companyReceivers").value = company.receivers || "";
  document.querySelector("#alertSeconds").value = company.alert_after_seconds || 30;
}

function healthClass(value) {
  if (value >= 95) return "good";
  if (value >= 75) return "warning";
  return "danger";
}

function renderBars(containerId, values, compact = false) {
  const container = document.querySelector(`#${containerId}`);
  if (!container) return;
  const safe = values.length ? values : [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
  container.innerHTML = safe.map((value, index) => compact ? `<span class="mini-bar" style="height:${Math.max(value, 8)}%"></span>` : `<div class="health-bar-wrap" title="Check ${index + 1}: ${value}% online"><div class="health-bar-track"><div class="health-bar ${healthClass(value)}" style="height:${Math.max(value, 6)}%"></div></div><span class="health-label">${value}%</span></div>`).join("");
}

function renderHealthBars() {
  const current = uptimeHistory[uptimeHistory.length - 1] || 0;
  document.querySelector("#currentHealth").textContent = `${current}%`;
  renderBars("healthBars", uptimeHistory);
}

function renderAll() {
  renderCompanySelect();
  renderDashboard();
  renderDevices();
  renderReports();
  renderCompanies();
}

function downloadCsv(filename, rows) {
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

document.querySelectorAll(".auth-tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".auth-tab").forEach((tab) => tab.classList.remove("active"));
  button.classList.add("active");
  const login = button.dataset.authTab === "login";
  document.querySelector("#loginForm").classList.toggle("hidden", !login);
  document.querySelector("#signupForm").classList.toggle("hidden", login);
}));

document.querySelector("#loginForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  authMessage.textContent = "";
  try {
    const data = await api("/api/login", { method: "POST", body: JSON.stringify({ email: loginEmail.value, password: loginPassword.value }) });
    currentUser = data.user;
    showApp();
  } catch (error) {
    authMessage.textContent = error.message;
  }
});

document.querySelector("#signupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  authMessage.textContent = "";
  try {
    const data = await api("/api/signup", { method: "POST", body: JSON.stringify({ name: signupName.value, email: signupEmail.value, password: signupPassword.value, company: signupCompany.value }) });
    currentUser = data.user;
    showApp();
  } catch (error) {
    authMessage.textContent = error.message;
  }
});

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
  document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
  button.classList.add("active");
  document.querySelector(`#${button.dataset.page}`).classList.add("active");
  pageTitle.textContent = button.textContent;
}));

companySelect.addEventListener("change", async () => { activeCompanyId = companySelect.value; activeReportIp = "all"; notificationDirty = false; await loadAll(); });
refreshBtn.addEventListener("click", loadAll);
logoutBtn.addEventListener("click", async () => { await api("/api/logout", { method: "POST" }); showAuth(); });

showAddDeviceBtn.addEventListener("click", () => addDeviceForm.classList.remove("hidden"));
cancelAddDeviceBtn.addEventListener("click", () => { addDeviceForm.reset(); addDeviceForm.classList.add("hidden"); });
addDeviceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await api(`/api/companies/${activeCompanyId}/devices`, { method: "POST", body: JSON.stringify({ location: newLocation.value, name: newDeviceName.value, ip: newIpAddress.value }) });
  addDeviceForm.reset();
  addDeviceForm.classList.add("hidden");
  await loadAll();
});

showImportCsvBtn.addEventListener("click", () => csvImportPanel.classList.toggle("hidden"));
importCsvBtn.addEventListener("click", async () => {
  const file = csvFileInput.files[0];
  if (!file) return alert("Choose a CSV file first.");
  const csv = await file.text();
  const result = await api(`/api/companies/${activeCompanyId}/devices/import`, { method: "POST", body: JSON.stringify({ csv }) });
  alert(`Imported ${result.imported} devices.`);
  csvFileInput.value = "";
  csvImportPanel.classList.add("hidden");
  await loadAll();
});

exportDevicesBtn.addEventListener("click", () => {
  const rows = ["Location,Name,IP", ...devices.map((d) => `${d.location},${d.name},${d.ip}`)];
  downloadCsv("devices.csv", rows);
});

downloadReportBtn.addEventListener("click", () => {
  const selected = activeReportIp === "all" ? reports : reports.filter((report) => report.ip === activeReportIp);
  const rows = ["Date,Location,Device Name,IP Address,Offline Time,Online Time,Downtime", ...selected.map((r) => `${r.date},${r.location},${r.name},${r.ip},${r.offline},${r.online},${r.downtime}`)];
  downloadCsv("outage_report.csv", rows);
});

showAddCompanyBtn.addEventListener("click", () => addCompanyForm.classList.remove("hidden"));
cancelAddCompanyBtn.addEventListener("click", () => { addCompanyForm.reset(); addCompanyForm.classList.add("hidden"); });
addCompanyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = await api("/api/companies", { method: "POST", body: JSON.stringify({ name: newCompanyName.value, email: newCompanyEmail.value, plan: newCompanyPlan.value }) });
  activeCompanyId = data.company.id;
  addCompanyForm.reset();
  addCompanyForm.classList.add("hidden");
  await loadAll();
});

saveNotificationBtn.addEventListener("click", async () => {
  const company = activeCompany();
  await api(`/api/companies/${company.id}`, { method: "PUT", body: JSON.stringify({ ...company, email: companyEmail.value, receivers: companyReceivers.value, alert_after_seconds: Number(alertSeconds.value) || 30 }) });
  notificationDirty = false;
  alert("Notification settings saved.");
  await loadAll();
});

[companyEmail, companyReceivers, alertSeconds].forEach((field) => {
  field.addEventListener("input", () => {
    notificationDirty = true;
  });
});

async function boot() {
  renderBars("authBars", [100, 92, 82, 100, 96, 88, 100, 76, 92, 100, 84, 100], true);
  const me = await api("/api/me");
  if (me.authenticated) {
    currentUser = me.user;
    showApp();
  }
}

boot();
