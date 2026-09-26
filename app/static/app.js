const q = selector => document.querySelector(selector);
const API = "/api/v1";
let csrfToken = null;
let eventFilter = "";
let refreshTimer = null;

const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
}[character]));

function errorMessage(body, status) {
  const detail = body?.detail ?? body?.error;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return `HTTP ${status}`;
}

async function api(path, options = {}) {
  const headers = {...(options.headers || {})};
  if (options.body) headers["Content-Type"] = "application/json";
  if (csrfToken && ["POST", "PUT", "PATCH", "DELETE"].includes(options.method || "GET")) {
    headers["X-CSRF-Token"] = csrfToken;
  }
  const response = await fetch(`${API}${path}`, {...options, headers});
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(errorMessage(body, response.status));
    error.status = response.status;
    throw error;
  }
  return body;
}

function actionButton(account, action, label = action.toUpperCase(), dangerous = false) {
  return `<button class="${dangerous ? "danger" : action === "start" ? "" : "muted"}" data-account="${account.id}" data-action="${action}">${label}</button>`;
}

function accountActions(account) {
  if (!account.enabled || account.status === "DISABLED") {
    return actionButton(account, "enable", "ENABLE");
  }
  const actions = [];
  if (!account.verified_at && !["NEW", "PROVISIONING"].includes(account.state)) {
    actions.push(actionButton(account, "view", "VIEW ONLY"));
    actions.push(actionButton(account, "view-interactive", "CONTROL VIEW"));
    if (account.state === "RUNNING") {
      actions.push(actionButton(account, "verify", "VERIFY"));
      actions.push(actionButton(account, "stop"));
    } else {
      actions.push(actionButton(account, "setup", "SETUP"));
    }
  } else if (["READY", "STOPPED"].includes(account.state)) {
    actions.push(actionButton(account, "start"));
    actions.push(actionButton(account, "disable", "DISABLE", true));
  } else if (account.state === "RUNNING") {
    actions.push(actionButton(account, "view", "VIEW ONLY"));
    actions.push(actionButton(account, "view-interactive", "CONTROL VIEW"));
    if (["PAUSED", "DISABLED", "STOPPED", "NEEDS_ATTENTION"].includes(account.worker_state)) {
      actions.push(actionButton(account, "worker-resume", "RESUME WORKER"));
    } else {
      actions.push(actionButton(account, "worker-pause", "PAUSE WORKER"));
    }
    actions.push(actionButton(account, "worker-stop", "STOP WORKER", true));
    if (account.worker_mode === "DISABLED") actions.push(actionButton(account, "worker-mode-observe", "MODE: OBSERVE"));
    if (account.worker_mode === "OBSERVE") actions.push(actionButton(account, "worker-mode-active", "MODE: ACTIVE"));
    if (account.worker_mode !== "DISABLED") actions.push(actionButton(account, "worker-mode-disabled", "DISABLE WORKER"));
    actions.push(actionButton(account, "stop"));
    actions.push(actionButton(account, "restart"));
  } else if (["ERROR", "STALE"].includes(account.state)) {
    actions.push(actionButton(account, "view", "VIEW ONLY"));
    actions.push(actionButton(account, "view-interactive", "CONTROL VIEW"));
    actions.push(actionButton(account, "start", "RETRY"));
    actions.push(actionButton(account, "stop", "STOP"));
    actions.push(actionButton(account, "rebuild", "REBUILD", true));
  } else {
    actions.push(`<button class="muted" disabled>${esc(account.state)}</button>`);
  }
  return actions.join("");
}

function accountCard(account) {
  const health = account.runtime_health || {};
  const heartbeat = account.last_heartbeat_at
    ? new Date(account.last_heartbeat_at).toLocaleString()
    : "never";
  const error = account.last_error_code
    ? `<div class="error account-error"><strong>${esc(account.last_error_code)}</strong> — ${esc(account.last_error_message)}</div>`
    : "";
  return `<article class="account">
    <div><strong>#${account.id} ${esc(account.label)}</strong><div class="meta">${esc(account.steam_username)} · ${esc(account.external_id)} · gen ${account.runtime_generation}</div></div>
    <div><div class="state ${esc(account.state)}">${esc(account.state)}</div><div class="meta">account ${esc(account.status)} · desired ${esc(account.desired_state)}</div></div>
    <div><div>${esc(account.node_name)} <span class="pill">${esc(account.node_status)}</span></div><div class="meta">Steam ${account.steam_running ? "on" : "off"} · DST ${account.dst_running ? "on" : "off"} · heartbeat ${esc(heartbeat)}</div></div>
    <div class="meta account-detail">Worker ${esc(account.worker_plugin)} ${esc(account.worker_version || "")} · mode ${esc(account.worker_mode)} · state ${esc(account.worker_state)} · last observation ${esc(account.worker_last_observation_at || "never")} · last action ${esc(account.worker_last_action || "—")}${account.worker_error_code ? ` · ${esc(account.worker_error_code)}` : ""}</div>
    <div class="meta account-detail">Container ${esc(health.container || account.state)} · Agent ${esc(health.agent || "OFFLINE")} · Display ${esc(health.display || "UNKNOWN")} · Steam ${esc(health.steam || "UNKNOWN")} · DST ${esc(health.dst || "UNKNOWN")} · Worker ${esc(health.worker || "NOOP")} · image ${esc(account.image_version)} · bootstrap v${esc(account.bootstrap_version)} ${esc(account.bootstrap_phase || "PENDING")}</div>
    <div class="actions">${accountActions(account)}</div>
    ${error}
  </article>`;
}

function bytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function nodeRow(node) {
  const resource = node.resources || {};
  const actions = node.maintenance
    ? `<button class="muted" data-node="${node.id}" data-node-action="exit-maintenance">EXIT MAINTENANCE</button>`
    : `<button class="muted" data-node="${node.id}" data-node-action="drain">DRAIN</button>
       <button class="muted" data-node="${node.id}" data-node-action="maintenance">MAINTENANCE</button>`;
  return `<div class="row node-row">
    <div><strong>${esc(node.name)}</strong><div class="meta">${esc(node.status)} · ${esc(node.provider)}</div></div>
    <span>${node.active_slots}/${node.max_active_slots} slots</span>
    <span>CPU ${resource.cpu_percent ?? "—"}% · RAM ${bytes(resource.ram_used_bytes)} / ${bytes(resource.ram_total_bytes)} · GPU ${resource.gpu_present === true ? "yes" : resource.gpu_present === false ? "no" : "unknown"}</span>
    <div class="actions">${actions}</div>
  </div>`;
}

function jobRow(job) {
  return `<div class="row job-row">
    <strong>#${job.id} ${esc(job.kind)}</strong>
    <span class="state ${esc(job.status)}">${esc(job.status)}</span>
    <span>attempt ${job.attempt_count}/${job.max_attempts} · account ${job.account_id ?? "—"} · ${esc(job.last_error_code || "")}</span>
  </div>`;
}

function eventRow(event) {
  return `<div class="row event-row">
    <span>${esc(new Date(event.created_at).toLocaleString())}</span>
    <strong class="${event.level === "ERROR" ? "error" : ""}">${esc(event.kind)}</strong>
    <span>${esc(event.message)}</span>
  </div>`;
}

async function load() {
  if (!csrfToken) return;
  try {
    const query = eventFilter === "ERROR"
      ? "?limit=50&level=ERROR"
      : eventFilter ? `?limit=50&entity=${eventFilter}` : "?limit=50";
    const [accounts, nodes, jobs, events, version] = await Promise.all([
      api("/accounts"),
      api("/nodes"),
      api("/jobs?limit=50"),
      api(`/events${query}`),
      api("/system/version")
    ]);
    q("#accountCount").textContent = accounts.length;
    q("#runningCount").textContent = accounts.filter(item => item.state === "RUNNING").length;
    q("#errorCount").textContent = accounts.filter(item => ["ERROR", "STALE"].includes(item.state) || item.status === "NEEDS_ATTENTION").length;
    q("#jobCount").textContent = jobs.filter(item => ["PENDING", "LEASED", "RUNNING", "RETRY"].includes(item.status)).length;
    q("#accounts").innerHTML = accounts.length ? accounts.map(accountCard).join("") : '<div class="empty">Аккаунтов пока нет.</div>';
    q("#nodes").innerHTML = nodes.length ? nodes.map(nodeRow).join("") : '<div class="empty">Nodes не зарегистрированы.</div>';
    q("#jobs").innerHTML = jobs.length ? jobs.map(jobRow).join("") : '<div class="empty">Jobs пока нет.</div>';
    q("#events").innerHTML = events.length ? events.map(eventRow).join("") : '<div class="empty">Событий по фильтру нет.</div>';
    q("#protocol").textContent = version.agent_protocol_version;
    q("#environment").textContent = `app ${version.app_version} / schema ${version.schema_version}`;
    q("#systemState").innerHTML = '<i class="status-dot"></i> SYSTEM READY';
  } catch (error) {
    if (error.status === 401) return showLogin();
    q("#systemState").textContent = "DEGRADED";
  }
}

async function session() {
  try {
    const state = await api("/auth/session");
    csrfToken = state.csrf_token;
    q("#addBtn").disabled = false;
    q("#logoutBtn").classList.remove("hidden");
    q("#loginDialog").close();
    startRefresh();
    await load();
  } catch (_error) {
    showLogin();
  }
}

function showLogin() {
  csrfToken = null;
  if (refreshTimer) clearInterval(refreshTimer);
  for (const selector of ["#accounts", "#nodes", "#jobs", "#events"]) q(selector).replaceChildren();
  q("#addBtn").disabled = true;
  q("#logoutBtn").classList.add("hidden");
  if (!q("#loginDialog").open) q("#loginDialog").showModal();
}

function startRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(load, 5000);
}

async function accountCommand(id, action) {
  if (action === "rebuild" && !confirm("Rebuild создаст новую runtime generation. Account сохранится, login session автоматически не переносится. Продолжить?")) return;
  if (action === "disable" && !confirm("Отключить остановленный аккаунт?")) return;
  if (action === "view") {
    const response = await api(`/runtimes/${id}/view-sessions`, {method: "POST"});
    alert(`VIEW session #${response.id} создана до ${response.expires_at}. Transport integration должен обменять одноразовый bearer token.`);
    return;
  }
  await api(`/accounts/${id}/${action}`, {method: "POST", body: action === "rebuild" ? "{}" : undefined});
}

async function openView(account, interactive) {
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  const created = await api(`/runtimes/${account.runtime_id}/view-sessions`, {
    method: "POST",
    body: JSON.stringify({mode: interactive ? "INTERACTIVE" : "VIEW_ONLY"})
  });
  await api(`/view-sessions/${created.id}/access`, {
    method: "POST",
    body: JSON.stringify({token: created.access_token})
  });
  let status = created;
  for (let attempt = 0; status.status === "CREATING" && attempt < 15; attempt += 1) {
    await new Promise(resolve => setTimeout(resolve, 1000));
    status = await api(`/view-sessions/${created.id}`, {
      headers: {Authorization: `Bearer ${created.access_token}`}
    });
  }
  if (status.status !== "ACTIVE" || !status.transport) {
    if (popup) popup.close();
    alert(`VIEW #${created.id}: ${status.status}. Interactive VIEW ждёт подтверждённого PAUSED worker.`);
    return;
  }
  if (popup) popup.location.replace(status.transport);
  else alert(`VIEW готов: ${status.transport}. Разрешите pop-up для панели управления.`);
}

q("#accounts").addEventListener("click", async event => {
  const button = event.target.closest("[data-account]");
  if (!button) return;
  try {
    const account = (await api(`/accounts/${button.dataset.account}`));
    if (["view", "view-interactive"].includes(button.dataset.action)) {
      await openView(account, button.dataset.action === "view-interactive");
    } else if (button.dataset.action.startsWith("worker-")) {
      const command = button.dataset.action.slice("worker-".length);
      if (command.startsWith("mode-")) {
        const mode = command.slice("mode-".length).toUpperCase();
        if (mode === "ACTIVE" && !confirm("ACTIVE отправляет bounded keyboard/mouse input. Продолжить?")) return;
        await api(`/accounts/${account.id}/worker/mode`, {method: "POST", body: JSON.stringify({mode})});
      } else {
        await api(`/accounts/${account.id}/worker/${command}`, {method: "POST"});
      }
    } else {
      await accountCommand(account.id, button.dataset.action);
    }
  } catch (error) {
    alert(error.message);
  }
  await load();
});

q("#nodes").addEventListener("click", async event => {
  const button = event.target.closest("[data-node-action]");
  if (!button) return;
  try {
    await api(`/nodes/${button.dataset.node}/${button.dataset.nodeAction}`, {method: "POST"});
  } catch (error) {
    alert(error.message);
  }
  await load();
});

q("#eventFilters").addEventListener("click", event => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  eventFilter = button.dataset.filter;
  q("#eventFilters").querySelectorAll("button").forEach(item => item.classList.toggle("active", item === button));
  load();
});

q("#loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  q("#loginError").textContent = "";
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    const response = await api("/auth/login", {method: "POST", body: JSON.stringify(payload)});
    csrfToken = response.csrf_token;
    q("#addBtn").disabled = false;
    q("#logoutBtn").classList.remove("hidden");
    q("#loginDialog").close();
    form.reset();
    startRefresh();
    await load();
  } catch (error) {
    q("#loginError").textContent = error.message;
  }
});

q("#logoutBtn").addEventListener("click", async () => {
  try { await api("/auth/logout", {method: "POST"}); } catch (_error) {}
  showLogin();
});

q("#addBtn").onclick = () => q("#accountDialog").showModal();
q("#closeAccount").onclick = () => q("#accountDialog").close();
q("#refresh").onclick = load;
q("#accountForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  q("#accountError").textContent = "";
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  for (const key in payload) if (payload[key] === "") payload[key] = null;
  try {
    await api("/accounts", {method: "POST", body: JSON.stringify(payload)});
    form.reset();
    q("#accountDialog").close();
    await load();
  } catch (error) {
    q("#accountError").textContent = error.message;
  }
});

// Compact data refresh for the control-plane layout.
async function load() {
  if (!csrfToken) return;
  try {
    const query = eventFilter === "ERROR"
      ? "?limit=50&level=ERROR"
      : eventFilter ? `?limit=50&entity=${eventFilter}` : "?limit=50";
    const [accounts, nodes, jobs, events, version] = await Promise.all([
      api("/accounts"),
      api("/nodes"),
      api("/jobs?limit=50"),
      api(`/events${query}`),
      api("/system/version")
    ]);
    q("#accountCount").textContent = accounts.length;
    q("#runningCount").textContent = accounts.filter(item => item.state === "RUNNING").length;
    q("#errorCount").textContent = accounts.filter(item => ["ERROR", "STALE"].includes(item.state) || item.status === "NEEDS_ATTENTION").length;
    q("#jobCount").textContent = jobs.filter(item => ["PENDING", "LEASED", "RUNNING", "RETRY"].includes(item.status)).length;
    q("#accounts").innerHTML = accounts.length ? accounts.map(accountCard).join("") : '<div class="empty">Нет аккаунтов.</div>';
    q("#nodes").innerHTML = nodes.length ? nodes.map(nodeRow).join("") : '<div class="empty">Нет нод.</div>';
    q("#jobs").innerHTML = jobs.length ? jobs.map(jobRow).join("") : '<div class="empty">Нет задач.</div>';
    q("#events").innerHTML = events.length ? events.map(eventRow).join("") : '<div class="empty">Нет событий.</div>';
    q("#protocol").textContent = version.agent_protocol_version;
    q("#environment").textContent = `app ${version.app_version} / schema ${version.schema_version}`;
    q("#systemState").textContent = "SYSTEM READY";
  } catch (error) {
    if (error.status === 401) return showLogin();
    q("#systemState").textContent = "DEGRADED";
  }
}

// Compact renderers for the control-plane layout.
function bytes(value) {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit > 1 ? 1 : 0)} ${units[unit]}`;
}

function accountCard(account) {
  const health = account.runtime_health || {};
  const heartbeat = account.last_heartbeat_at ? new Date(account.last_heartbeat_at).toLocaleString() : "never";
  const error = account.last_error_code
    ? `<div class="error account-error"><strong>${esc(account.last_error_code)}</strong> — ${esc(account.last_error_message)}</div>`
    : "";
  return `<article class="account">
    <div><strong>#${account.id} ${esc(account.label)}</strong><div class="meta">${esc(account.steam_username)} · ${esc(account.external_id)} · gen ${account.runtime_generation}</div></div>
    <div><div class="state ${esc(account.state)}">${esc(account.state)}</div><div class="meta">${esc(account.status)} · desired ${esc(account.desired_state)}</div></div>
    <div><div>${esc(account.node_name)} <span class="pill">${esc(account.node_status)}</span></div><div class="meta">Steam ${account.steam_running ? "on" : "off"} · DST ${account.dst_running ? "on" : "off"} · ${esc(heartbeat)}</div></div>
    <div class="meta account-detail">Worker ${esc(account.worker_plugin)} · ${esc(account.worker_mode)} · ${esc(account.worker_state)} · last action ${esc(account.worker_last_action || "—")}${account.worker_error_code ? ` · ${esc(account.worker_error_code)}` : ""}</div>
    <div class="meta account-detail">Container ${esc(health.container || account.state)} · Agent ${esc(health.agent || "OFFLINE")} · Display ${esc(health.display || "UNKNOWN")} · Steam ${esc(health.steam || "UNKNOWN")} · DST ${esc(health.dst || "UNKNOWN")} · Worker ${esc(health.worker || "NOOP")} · image ${esc(account.image_version)} · bootstrap v${esc(account.bootstrap_version)} ${esc(account.bootstrap_phase || "PENDING")}</div>
    <div class="actions">${accountActions(account)}</div>
    ${error}
  </article>`;
}

function nodeRow(node) {
  const resource = node.resources || {};
  const actions = node.maintenance
    ? `<button class="muted" data-node="${node.id}" data-node-action="exit-maintenance">EXIT MAINTENANCE</button>`
    : `<button class="muted" data-node="${node.id}" data-node-action="drain">DRAIN</button><button class="muted" data-node="${node.id}" data-node-action="maintenance">MAINTENANCE</button>`;
  return `<div class="row node-row">
    <div><strong>${esc(node.name)}</strong><div class="meta">${esc(node.status)} · ${esc(node.provider)}</div></div>
    <span>${node.active_slots}/${node.max_active_slots} slots</span>
    <span>CPU ${resource.cpu_percent ?? "—"}% · RAM ${bytes(resource.ram_used_bytes)} / ${bytes(resource.ram_total_bytes)} · GPU ${resource.gpu_present === true ? "yes" : resource.gpu_present === false ? "no" : "unknown"}</span>
    <div class="actions">${actions}</div>
  </div>`;
}

function jobRow(job) {
  return `<div class="row job-row">
    <strong>#${job.id} ${esc(job.kind)}</strong>
    <span class="state ${esc(job.status)}">${esc(job.status)}</span>
    <span>attempt ${job.attempt_count}/${job.max_attempts} · account ${job.account_id ?? "—"} · ${esc(job.last_error_code || "")}</span>
  </div>`;
}

function eventRow(event) {
  return `<div class="row event-row">
    <span>${esc(new Date(event.created_at).toLocaleString())}</span>
    <strong class="${event.level === "ERROR" ? "error" : ""}">${esc(event.kind)}</strong>
    <span>${esc(event.message)}</span>
  </div>`;
}

session();
