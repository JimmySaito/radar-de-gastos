// UsageHub: SPA sin build step. Lee /config.json (generado por Terraform o el servidor local).
// Rutas: #/real (datos del tenant), #/ejemplo (tenant de demostración, solo lectura),
//        #/configuracion/{conexiones|personas|presupuestos|ingesta}.
// authMode "cognito": OAuth 2.0 Authorization Code + PKCE.  authMode "local": servidor local.

const PROVIDER_ORDER = ["anthropic", "openai", "google", "aws", "claude_code", "custom", "synthetic"];
const PROVIDER_LABEL = { anthropic: "Anthropic (Claude)", openai: "OpenAI", google: "Google (Gemini)", aws: "AWS", claude_code: "Claude Code (estimado)", custom: "Personalizado", synthetic: "Generador de prueba", subscription: "Suscripción" };
const state = { days: 30, groupBy: "provider", config: null, me: null, charts: {}, catalog: null, editing: null };

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const seriesColor = (i) => css(`--series-${(i % 8) + 1}`);
const providerColor = (p) => seriesColor(Math.max(PROVIDER_ORDER.indexOf(p), 0));
const usd = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("es", { notation: "compact", maximumFractionDigits: 1 });
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const providerName = (p) => PROVIDER_LABEL[p] || p;

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = `toast${isError ? " error" : ""}`;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 7000 : 3500);
}

function relativeTime(iso) {
  if (!iso) return "nunca";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const rtf = new Intl.RelativeTimeFormat("es", { numeric: "auto" });
  if (seconds < 60) return rtf.format(-seconds, "second");
  if (seconds < 3600) return rtf.format(-Math.round(seconds / 60), "minute");
  if (seconds < 86400) return rtf.format(-Math.round(seconds / 3600), "hour");
  return rtf.format(-Math.round(seconds / 86400), "day");
}

// ------------------------------------------------------------------ auth (PKCE)
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const sha256 = (text) => crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
function storage() { try { return window.sessionStorage; } catch { return null; } }

async function login(cfg) {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const stateParam = b64url(crypto.getRandomValues(new Uint8Array(16)));
  storage()?.setItem("pkce", JSON.stringify({ verifier, state: stateParam, hash: location.hash }));
  const params = new URLSearchParams({
    response_type: "code", client_id: cfg.clientId, redirect_uri: location.origin + "/",
    scope: "openid email profile", code_challenge_method: "S256",
    code_challenge: b64url(await sha256(verifier)), state: stateParam,
  });
  location.assign(`${cfg.cognitoDomain}/oauth2/authorize?${params}`);
}

async function exchangeCode(cfg, code, returnedState) {
  const saved = JSON.parse(storage()?.getItem("pkce") || "{}");
  if (!saved.verifier || saved.state !== returnedState) throw new Error("estado OAuth inválido");
  const res = await fetch(`${cfg.cognitoDomain}/oauth2/token`, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: cfg.clientId, code, redirect_uri: location.origin + "/", code_verifier: saved.verifier }),
  });
  if (!res.ok) throw new Error("no se pudo obtener el token");
  const tokens = await res.json();
  storage()?.setItem("tokens", JSON.stringify({ ...tokens, expires_at: Date.now() + tokens.expires_in * 1000 }));
  storage()?.removeItem("pkce");
  history.replaceState({}, "", "/" + (saved.hash || ""));
}

async function accessToken(cfg) {
  if (cfg.authMode !== "cognito") return null;
  const q = new URLSearchParams(location.search);
  if (q.get("code")) await exchangeCode(cfg, q.get("code"), q.get("state"));
  const tokens = JSON.parse(storage()?.getItem("tokens") || "null");
  if (!tokens || tokens.expires_at < Date.now() + 30_000) { await login(cfg); return new Promise(() => {}); }
  $("logout").hidden = false;
  $("logout").onclick = () => {
    storage()?.removeItem("tokens");
    location.assign(`${cfg.cognitoDomain}/logout?client_id=${cfg.clientId}&logout_uri=${encodeURIComponent(location.origin + "/")}`);
  };
  return tokens.id_token; // incluye custom:tenant_id y cognito:groups
}

async function api(path, { method = "GET", params = {}, body } = {}) {
  const cfg = state.config;
  const headers = {};
  const token = await accessToken(cfg);
  if (token) headers.authorization = `Bearer ${token}`;
  if (cfg.authMode === "local") headers["x-tenant-id"] = cfg.tenant || "local";
  if (body !== undefined) headers["content-type"] = "application/json";
  const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== ""));
  const res = await fetch(`${cfg.apiBaseUrl}${path}${query.size ? `?${query}` : ""}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    let message = data?.message || `HTTP ${res.status}`;
    try { const parsed = JSON.parse(message); if (Array.isArray(parsed)) message = parsed.map((e) => `${e.loc?.slice(-1)[0] ?? ""}: ${e.msg}`).join("; "); } catch { /* mensaje plano */ }
    throw new Error(message);
  }
  return data;
}

// ------------------------------------------------------------------ router
function route() {
  const [, section = "real", tab = "conexiones"] = (location.hash || "#/real").split("/");
  return { section, tab };
}

async function render() {
  const { section, tab } = route();
  const isSettings = section === "configuracion";
  const isDemo = section === "ejemplo";
  document.querySelectorAll("[data-view]").forEach((a) => {
    const active = !isSettings && a.dataset.view === (isDemo ? "ejemplo" : "real");
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  $("dashboard").hidden = isSettings;
  $("settings").hidden = !isSettings;
  $("filters").hidden = isSettings;
  $("settingsLink").hidden = !state.me?.is_admin;
  if (isSettings) $("settingsLink").setAttribute("aria-current", "page"); else $("settingsLink").removeAttribute("aria-current");
  $("banner").hidden = true;
  if (isSettings) await renderSettings(tab);
  else await renderDashboard(isDemo);
}

// ------------------------------------------------------------------ dashboard
function range() {
  const end = new Date(); end.setUTCDate(end.getUTCDate() + 1);
  const start = new Date(end); start.setUTCDate(start.getUTCDate() - state.days);
  const iso = (d) => d.toISOString().slice(0, 10);
  return { from: iso(start), to: iso(end) };
}

function baseOptions(extra = {}) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "top", align: "start", labels: { color: css("--text-secondary"), boxWidth: 10, boxHeight: 10, useBorderRadius: true, borderRadius: 3 } },
      tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${usd.format(ctx.parsed.y ?? ctx.parsed.x)}` } },
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: css("--text-muted"), maxRotation: 0, autoSkipPadding: 16 }, border: { color: css("--border") } },
      y: { grid: { color: css("--grid") }, ticks: { color: css("--text-muted"), callback: (v) => usd0.format(v) }, border: { display: false } },
    },
    ...extra,
  };
}

function draw(id, config) {
  state.charts[id]?.destroy();
  if (typeof Chart === "undefined") return; // CDN bloqueado: las tablas siguen disponibles
  state.charts[id] = new Chart($(id), config);
}

function renderKpis(summary) {
  const t = summary.total;
  $("kpiCost").textContent = usd.format(Number(t.cost));
  $("kpiRange").textContent = `${summary.from} → ${summary.to}`;
  $("kpiTokens").textContent = compact.format(t.total_tokens);
  $("kpiTokensSub").textContent = `${compact.format(t.input_tokens)} entrada · ${compact.format(t.output_tokens)} salida`;
  $("kpiRequests").textContent = compact.format(t.requests);
  const team = summary.top_teams.reduce((acc, r) => { acc[r.name] = (acc[r.name] || 0) + Number(r.cost); return acc; }, {});
  const [topTeam, topCost] = Object.entries(team).sort((a, b) => b[1] - a[1])[0] || ["–", 0];
  $("kpiTeam").textContent = topTeam === "unassigned" ? "sin asignar" : topTeam;
  $("kpiTeamSub").textContent = topCost ? usd.format(topCost) : "";
}

function renderProviders(summary) {
  const total = Number(summary.total.cost) || 1;
  const rows = Object.entries(summary.by_provider).sort((a, b) => PROVIDER_ORDER.indexOf(a[0]) - PROVIDER_ORDER.indexOf(b[0]));
  $("providersTable").tBodies[0].innerHTML = rows.map(([p, m]) => `
    <tr><td><span class="swatch" style="background:${providerColor(p)}"></span>${esc(providerName(p))}</td>
    <td class="num">${usd.format(Number(m.cost))}</td><td class="num">${compact.format(m.total_tokens)}</td>
    <td class="num">${(100 * Number(m.cost) / total).toFixed(1)}%</td></tr>`).join("") || '<tr><td colspan="4" class="muted">Sin datos en el rango</td></tr>';
}

const ICONS = {
  ok: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--status-good)"/><path d="M4.5 8.2l2.2 2.2 4.8-4.8" stroke="#fff" stroke-width="1.8" fill="none"/></svg>',
  warn: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l7 13H1z" fill="var(--status-warning)"/><path d="M8 6v4M8 11.5v.5" stroke="#000" stroke-width="1.6"/></svg>',
  over: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--status-critical)"/><path d="M5 5l6 6M11 5l-6 6" stroke="#fff" stroke-width="1.8"/></svg>',
};
const budgetState = (ratio) => (ratio >= 1 ? ["over", "Excedido"] : ratio >= 0.8 ? ["warn", "En riesgo"] : ["ok", "En rango"]);

function renderBudgets(budgets) {
  $("budgetsTable").tBodies[0].innerHTML = budgets.items.map((b) => {
    const [icon, label] = budgetState(b.ratio);
    return `<tr><td>${esc(b.team)}${b.provider ? ` · ${esc(providerName(b.provider))}` : ""}</td><td class="num">${usd.format(Number(b.spent))}</td>
      <td class="num">${usd0.format(Number(b.monthly_limit))}</td>
      <td><span class="status">${ICONS[icon]}${label} (${Math.round(b.ratio * 100)}%)</span></td></tr>`;
  }).join("") || '<tr><td colspan="4" class="muted">Sin presupuestos configurados</td></tr>';
}

function renderSeries(ts) {
  const ordered = [...ts.series].sort((a, b) => (state.groupBy === "provider" ? PROVIDER_ORDER.indexOf(a.name) - PROVIDER_ORDER.indexOf(b.name) : a.name.localeCompare(b.name)));
  const datasets = ordered.slice(0, 8).map((s, i) => ({
    label: state.groupBy === "provider" ? providerName(s.name) : s.name,
    data: s.cost.map(Number),
    backgroundColor: state.groupBy === "provider" ? providerColor(s.name) : seriesColor(i),
    borderColor: css("--surface-1"), borderWidth: { top: 2 }, borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 28,
  }));
  $("seriesTitle").textContent = `Costo diario por ${state.groupBy === "provider" ? "proveedor" : "equipo"} (USD)`;
  const opts = baseOptions();
  draw("seriesChart", { type: "bar", data: { labels: ts.days.map((d) => d.slice(5)), datasets },
    options: { ...opts, scales: { x: { ...opts.scales.x, stacked: true }, y: { ...opts.scales.y, stacked: true } } } });
  $("series-table").innerHTML = `<table class="data"><thead><tr><th>Día</th>${datasets.map((d) => `<th class="num">${esc(d.label)}</th>`).join("")}</tr></thead>
    <tbody>${ts.days.map((day, i) => `<tr><td>${day}</td>${datasets.map((d) => `<td class="num">${usd.format(d.data[i])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderRanking(id, rows) {
  const grouped = {};
  for (const r of rows) {
    grouped[r.name] ??= {};
    grouped[r.name][r.provider] = (grouped[r.name][r.provider] || 0) + Number(r.cost);
  }
  const sum = (n) => Object.values(grouped[n]).reduce((x, y) => x + y, 0);
  const names = Object.keys(grouped).sort((a, b) => sum(b) - sum(a)).slice(0, 8);
  const providers = PROVIDER_ORDER.filter((p) => names.some((n) => grouped[n][p]));
  const opts = baseOptions();
  draw(id, {
    type: "bar",
    data: { labels: names.map((n) => n.replace(/@.*/, "")), datasets: providers.map((p) => ({
      label: providerName(p), data: names.map((n) => grouped[n][p] || 0), backgroundColor: providerColor(p),
      borderColor: css("--surface-1"), borderWidth: { right: 2 }, borderRadius: 4, borderSkipped: "left", maxBarThickness: 18 })) },
    options: {
      ...opts, indexAxis: "y", interaction: { mode: "index", intersect: false, axis: "y" },
      plugins: { ...opts.plugins, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${usd.format(c.parsed.x)}` } } },
      scales: {
        x: { stacked: true, grid: { color: css("--grid") }, ticks: { color: css("--text-muted"), callback: (v) => usd0.format(v) }, border: { display: false } },
        y: { stacked: true, grid: { display: false }, ticks: { color: css("--text-secondary") }, border: { color: css("--border") } },
      },
    },
  });
}

async function renderDashboard(isDemo) {
  const dataset = isDemo ? "demo" : undefined;
  const r = range();
  $("demoNotice").hidden = !isDemo;
  try {
    const [summary, ts, budgets] = await Promise.all([
      api("/v1/summary", { params: { ...r, dataset } }),
      api("/v1/timeseries", { params: { ...r, group_by: state.groupBy, dataset } }),
      api("/v1/budgets", { params: { dataset } }),
    ]);
    const empty = !isDemo && Number(summary.total.cost) === 0 && summary.total.total_tokens === 0 && summary.total.requests === 0;
    $("emptyState").hidden = !empty;
    $("dashContent").hidden = empty && (state.me?.connections ?? 0) === 0;
    if (empty && state.me?.connections) {
      $("emptyState").querySelector("h1").textContent = "Aún no hay consumo en este rango";
      $("emptyState").querySelector("p").textContent = "Tus conexiones están configuradas. Si acabas de agregarlas, sincronízalas desde Configuración (los proveedores publican con 4–24 h de retraso) o amplía el rango de fechas.";
    }
    $("budgetsLink").hidden = isDemo || !state.me?.is_admin;
    renderKpis(summary); renderProviders(summary); renderBudgets(budgets); renderSeries(ts);
    renderRanking("membersChart", summary.top_members);
    renderRanking("servicesChart", summary.top_services);
  } catch (err) {
    $("banner").hidden = false;
    $("banner").textContent = `No se pudieron cargar los datos: ${err.message}`;
  }
}

// ------------------------------------------------------------------ configuración
async function renderSettings(tab) {
  if (!state.me?.is_admin) {
    $("banner").hidden = false;
    $("banner").textContent = "Necesitas pertenecer al grupo 'admins' para configurar conexiones.";
    return;
  }
  document.querySelectorAll("[data-tab]").forEach((a) => (a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  document.querySelectorAll("[data-panel]").forEach((p) => { p.hidden = p.dataset.panel !== tab; });
  const panel = document.querySelector(`[data-panel="${tab}"]`);
  if (!panel) return;
  panel.innerHTML = '<p class="muted">Cargando…</p>';
  try {
    if (tab === "conexiones") await renderConnections(panel);
    else if (tab === "personas") await renderPeople(panel);
    else if (tab === "presupuestos") await renderBudgetAdmin(panel);
    else if (tab === "ingesta") renderIngest(panel);
  } catch (err) {
    panel.innerHTML = `<p class="banner error">No se pudo cargar: ${esc(err.message)}</p>`;
  }
}

async function catalog() {
  state.catalog ??= (await api("/v1/providers")).items;
  return state.catalog;
}

function statusChip(status) {
  if (!status) return '<span class="chip idle">Sin sincronizar</span>';
  return status.ok ? '<span class="chip ok">Conectado</span>' : '<span class="chip bad">Error</span>';
}

async function renderConnections(panel) {
  const [items, specs] = await Promise.all([api("/v1/connectors"), catalog()]);
  const list = items.items;
  panel.innerHTML = `
    <div class="panel-head"><h2>Conexiones</h2><button class="btn primary" id="addConn" type="button">+ Agregar conexión</button></div>
    <div id="connForm"></div>
    <div class="stack" id="connList">
      ${list.length ? "" : '<div class="card"><p class="muted" style="margin:0">Todavía no hay conexiones. Agrega una para empezar a ver tus datos reales.</p></div>'}
      ${list.map((c) => {
        const s = c.status;
        const meta = s
          ? `Última sincronización ${relativeTime(s.last_sync_at)} · ${s.written} registros nuevos o revisados · ventana ${s.window_start} → ${s.window_end}`
          : "Aún no se ha sincronizado";
        return `<article class="card conn" data-id="${esc(c.connector_id)}">
          <div>
            <h3>${esc(c.label)} ${statusChip(s)} ${c.enabled ? "" : '<span class="chip">Pausada</span>'}</h3>
            <div class="meta">${esc(providerName(c.provider))} · relee los últimos ${c.lookback_days} días cada hora · ${esc(meta)}</div>
            ${s && !s.ok ? `<div class="err">${esc(s.error)}</div>` : ""}
          </div>
          <div class="row">
            <button class="btn small" data-act="sync" type="button">Sincronizar</button>
            <button class="btn small" data-act="edit" type="button">Editar</button>
            <button class="btn small danger" data-act="delete" type="button">Eliminar</button>
          </div>
        </article>`;
      }).join("")}
    </div>`;
  $("addConn").onclick = () => showProviderPicker(specs);
  panel.querySelectorAll(".conn").forEach((card) => {
    const conn = list.find((c) => c.connector_id === card.dataset.id);
    card.querySelector('[data-act="edit"]').onclick = () => showConnectionForm(specs.find((s) => s.provider === conn.provider), conn);
    card.querySelector('[data-act="sync"]').onclick = (e) => syncConnection(conn, e.currentTarget);
    card.querySelector('[data-act="delete"]').onclick = async () => {
      if (!confirm(`¿Eliminar la conexión "${conn.label}"? Se borrarán sus credenciales guardadas. Los datos ya cargados se conservan.`)) return;
      try { await api(`/v1/connectors/${encodeURIComponent(conn.connector_id)}`, { method: "DELETE" }); toast("Conexión eliminada"); render(); }
      catch (err) { toast(err.message, true); }
    };
  });
  if (!list.length) showProviderPicker(specs);
}

async function syncConnection(conn, button, days) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = "Sincronizando…";
  try {
    const r = await api(`/v1/connectors/${encodeURIComponent(conn.connector_id)}/sync`, { method: "POST", params: { days } });
    toast(state.config.authMode === "local" ? `Sincronización terminada (${r.from} → ${r.to})` : `Sincronización en cola (${r.from} → ${r.to}). Los datos aparecerán en unos segundos.`);
  } catch (err) { toast(err.message, true); }
  button.disabled = false;
  button.textContent = original;
  render();
}

function showProviderPicker(specs) {
  $("connForm").innerHTML = `<section class="card" style="margin-bottom:12px">
    <div class="card-head"><h2>¿Qué quieres conectar?</h2><button class="link" type="button" id="cancelPick">Cancelar</button></div>
    <div class="providers">${specs.map((s) => `<button type="button" class="provider-option" data-provider="${esc(s.provider)}"><strong>${esc(s.name)}</strong><span>${esc(s.description)}</span></button>`).join("")}</div>
    <p class="muted" style="margin:12px 0 0">¿Otro sistema? Envíalo por API en <a href="#/configuracion/ingesta">Ingesta por API</a>.</p>
  </section>`;
  $("cancelPick").onclick = () => { $("connForm").innerHTML = ""; };
  $("connForm").querySelectorAll("[data-provider]").forEach((b) => { b.onclick = () => showConnectionForm(specs.find((s) => s.provider === b.dataset.provider)); });
}

function fieldInput(f, conn) {
  const saved = conn?.secrets_set?.includes(f.name);
  const value = f.secret ? "" : (conn?.options?.[f.name] ?? f.default ?? "");
  const placeholder = f.secret && saved ? "•••••••• guardado — deja vacío para conservarlo" : f.placeholder;
  const common = `id="f_${esc(f.name)}" name="${esc(f.name)}" placeholder="${esc(placeholder)}" ${f.required && !(f.secret && saved) ? "required" : ""} autocomplete="off"`;
  const types = { password: "password", number: "number", date: "date" };
  const input = f.kind === "textarea"
    ? `<textarea ${common} spellcheck="false">${esc(value)}</textarea>`
    : f.kind === "select"
      ? `<select id="f_${esc(f.name)}" name="${esc(f.name)}">${f.choices.map(([v, label]) => `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`
      : `<input ${common} type="${types[f.kind] || "text"}" ${f.kind === "number" ? 'step="any" min="0"' : ""} value="${esc(value)}">`;
  return `<div class="field"><label for="f_${esc(f.name)}">${esc(f.label)}${f.required ? "" : ' <span class="muted">(opcional)</span>'}</label>${input}${f.help ? `<div class="help">${esc(f.help)}</div>` : ""}</div>`;
}

function showConnectionForm(spec, conn = null) {
  $("connForm").innerHTML = `<section class="card" style="margin-bottom:12px">
    <div class="card-head"><h2>${conn ? "Editar" : "Nueva"} conexión · ${esc(spec.name)}</h2><button class="link" type="button" id="cancelForm">Cancelar</button></div>
    <form class="form" id="connEdit" novalidate>
      <p class="muted" style="margin:0">${esc(spec.description)} ${spec.notes ? esc(spec.notes) : ""} ${spec.docs_url ? `<a class="link" href="${esc(spec.docs_url)}" target="_blank" rel="noopener noreferrer">Documentación ↗</a>` : ""}</p>
      <div class="fields-2">
        <div class="field"><label for="f_label">Nombre de la conexión</label><input id="f_label" name="label" value="${esc(conn?.label ?? spec.name)}" maxlength="120"></div>
        <div class="field"><label for="f_lookback">Días que se releen en cada sincronización</label><input id="f_lookback" name="lookback_days" type="number" min="1" max="31" value="${conn?.lookback_days ?? 3}"><div class="help">Los proveedores corrigen sus cifras hasta 24 h después; 3 días es un buen valor.</div></div>
      </div>
      ${spec.fields.map((f) => fieldInput(f, conn)).join("")}
      <div class="result" id="testResult" hidden></div>
      <div class="row">
        <button class="btn" type="button" id="testBtn">Probar conexión</button>
        <button class="btn primary" type="submit" id="saveBtn">${conn ? "Guardar cambios" : "Guardar y sincronizar"}</button>
      </div>
      <p class="muted" style="margin:0;font-size:12px">Las credenciales se guardan cifradas en el servidor (Secrets Manager en AWS, <code>~/.usagehub</code> en local) y nunca se vuelven a mostrar.</p>
    </form></section>`;
  $("connForm").scrollIntoView({ behavior: "smooth", block: "start" });
  $("cancelForm").onclick = () => { $("connForm").innerHTML = ""; };
  const form = $("connEdit");
  const payload = () => {
    const data = new FormData(form);
    const values = {};
    for (const f of spec.fields) values[f.name] = (data.get(f.name) || "").toString();
    return { connector_id: conn?.connector_id ?? null, provider: spec.provider, label: (data.get("label") || "").toString(),
             lookback_days: Number(data.get("lookback_days") || 3), enabled: conn?.enabled ?? true, values };
  };
  $("testBtn").onclick = async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Probando…";
    const out = $("testResult"); out.hidden = true;
    try {
      const r = await api("/v1/connectors/test", { method: "POST", body: payload() });
      out.className = `result ${r.ok ? "ok" : "bad"}`;
      out.textContent = r.ok
        ? `✓ Conexión correcta: ${r.records} registros y ${usd.format(Number(r.cost))} entre ${r.window[0]} y ${r.window[1]}.${r.records === 0 ? " (Puede ser normal si los datos aún no se publican.)" : ""}`
        : `✗ ${r.error}`;
      out.hidden = false;
    } catch (err) { out.className = "result bad"; out.textContent = `✗ ${err.message}`; out.hidden = false; }
    btn.disabled = false; btn.textContent = "Probar conexión";
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("saveBtn"); btn.disabled = true; btn.textContent = "Guardando…";
    try {
      const saved = await api("/v1/connectors", { method: "PUT", body: payload() });
      toast("Conexión guardada");
      state.me.connections = (await api("/v1/me")).connections;
      if (!conn) {
        btn.textContent = "Sincronizando 30 días…";
        await api(`/v1/connectors/${encodeURIComponent(saved.connector_id)}/sync`, { method: "POST", params: { days: 30 } });
        toast("Conexión guardada y sincronizada. Revisa tu dashboard de datos reales.");
      }
      render();
    } catch (err) { toast(err.message, true); btn.disabled = false; btn.textContent = conn ? "Guardar cambios" : "Guardar y sincronizar"; }
  };
}

async function renderPeople(panel) {
  const end = new Date(); end.setUTCDate(end.getUTCDate() + 1);
  const start = new Date(end); start.setUTCDate(start.getUTCDate() - 90);
  const [ids, maps, conns] = await Promise.all([
    api("/v1/identities", { params: { from: start.toISOString().slice(0, 10), to: end.toISOString().slice(0, 10) } }),
    api("/v1/mappings"), api("/v1/connectors"),
  ]);
  const teams = [...new Set(maps.items.map((m) => m.team))].sort();
  const unmapped = ids.items.filter((r) => !r.mapped);
  const totalCost = ids.items.reduce((a, r) => a + Number(r.cost), 0) || 1;
  const coverage = 100 - (100 * unmapped.reduce((a, r) => a + Number(r.cost), 0)) / totalCost;
  panel.innerHTML = `
    <div class="panel-head"><h2>Personas y equipos</h2>
      <button class="btn small" type="button" id="resyncAll" ${conns.items.length ? "" : "disabled"}>Reaplicar a los datos (resincronizar)</button></div>
    <p class="muted">Cada proveedor identifica el consumo con una identidad técnica (usuario, API key, proyecto, tag). Asígnala a una persona y a un equipo para ver el gasto por responsable.
    ${ids.items.length ? `Cobertura actual: <strong>${coverage.toFixed(1)}%</strong> del gasto de los últimos 90 días está asignado.` : ""}</p>
    <datalist id="teamList">${teams.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
    <section class="card" style="margin-bottom:12px">
      <div class="card-head"><h2>Identidades sin asignar (${unmapped.length})</h2></div>
      <div class="table-scroll"><table class="data"><thead><tr><th>Proveedor</th><th>Identidad</th><th class="num">Costo 90 d</th><th>Persona</th><th>Equipo</th><th></th></tr></thead><tbody>
      ${unmapped.map((r, i) => `<tr data-i="${i}"><td>${esc(providerName(r.provider))}</td><td><code>${esc(r.name)}</code></td><td class="num">${usd.format(Number(r.cost))}</td>
        <td><input data-k="member" placeholder="nombre@empresa.com"></td><td><input data-k="team" list="teamList" placeholder="equipo"></td>
        <td><button class="btn small primary" data-act="assign" type="button">Asignar</button></td></tr>`).join("") || '<tr><td colspan="6" class="muted">Todo el consumo está asignado 🎉</td></tr>'}
      </tbody></table></div>
    </section>
    <section class="card" style="margin-bottom:12px">
      <div class="card-head"><h2>Asignaciones (${maps.items.length})</h2></div>
      <div class="table-scroll"><table class="data"><thead><tr><th>Proveedor</th><th>Identidad</th><th>Persona</th><th>Equipo</th><th></th></tr></thead><tbody>
      ${maps.items.map((m, i) => `<tr data-m="${i}"><td>${esc(providerName(m.provider))}</td><td><code>${esc(m.principal)}</code></td><td>${esc(m.member)}</td><td>${esc(m.team)}</td>
        <td><button class="btn small danger" data-act="unmap" type="button">Quitar</button></td></tr>`).join("") || '<tr><td colspan="5" class="muted">Sin asignaciones</td></tr>'}
      </tbody></table></div>
    </section>
    <section class="card">
      <div class="card-head"><h2>Asignación manual</h2></div>
      <form class="inline-form" id="manualMap">
        <div class="field"><label for="m_provider">Proveedor</label><select id="m_provider" name="provider">${PROVIDER_ORDER.map((p) => `<option value="${p}">${esc(providerName(p))}</option>`).join("")}</select></div>
        <div class="field"><label for="m_principal">Identidad</label><input id="m_principal" name="principal" required placeholder="user:… / api_key:… / tag:…"></div>
        <div class="field"><label for="m_member">Persona</label><input id="m_member" name="member" required></div>
        <div class="field"><label for="m_team">Equipo</label><input id="m_team" name="team" list="teamList" required></div>
        <button class="btn primary" type="submit">Agregar</button>
      </form>
    </section>`;
  const save = async (mapping) => { await api("/v1/mappings", { method: "PUT", body: mapping }); toast("Asignación guardada. Resincroniza para aplicarla a los datos ya cargados."); render(); };
  panel.querySelectorAll('[data-act="assign"]').forEach((b) => {
    b.onclick = async () => {
      const tr = b.closest("tr"); const r = unmapped[Number(tr.dataset.i)];
      const member = tr.querySelector('[data-k="member"]').value.trim(); const team = tr.querySelector('[data-k="team"]').value.trim();
      if (!member || !team) { toast("Completa persona y equipo", true); return; }
      try { await save({ provider: r.provider, principal: r.name, member, team }); } catch (err) { toast(err.message, true); }
    };
  });
  panel.querySelectorAll('[data-act="unmap"]').forEach((b) => {
    b.onclick = async () => {
      const m = maps.items[Number(b.closest("tr").dataset.m)];
      try { await api("/v1/mappings", { method: "DELETE", params: { provider: m.provider, principal: m.principal } }); toast("Asignación eliminada"); render(); }
      catch (err) { toast(err.message, true); }
    };
  });
  $("manualMap").onsubmit = async (e) => {
    e.preventDefault();
    try { await save(Object.fromEntries(new FormData(e.currentTarget))); } catch (err) { toast(err.message, true); }
  };
  $("resyncAll").onclick = async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Resincronizando…";
    for (const c of conns.items) { try { await api(`/v1/connectors/${encodeURIComponent(c.connector_id)}/sync`, { method: "POST", params: { days: 31 } }); } catch (err) { toast(`${c.label}: ${err.message}`, true); } }
    toast("Asignaciones aplicadas a los últimos 31 días");
    render();
  };
}

async function renderBudgetAdmin(panel) {
  const [budgets, maps] = await Promise.all([api("/v1/budgets"), api("/v1/mappings")]);
  const teams = [...new Set(maps.items.map((m) => m.team))].sort();
  panel.innerHTML = `
    <div class="panel-head"><h2>Presupuestos mensuales</h2></div>
    <p class="muted">Se envía una alerta al llegar al 50 %, 80 % y 100 % del presupuesto (una sola vez por umbral y mes).</p>
    <section class="card" style="margin-bottom:12px"><div class="table-scroll"><table class="data">
      <thead><tr><th>Equipo</th><th>Proveedor</th><th class="num">Gastado</th><th class="num">Límite</th><th>Estado</th><th></th></tr></thead><tbody>
      ${budgets.items.map((b, i) => { const [icon, label] = budgetState(b.ratio); return `<tr data-i="${i}"><td>${esc(b.team)}</td><td>${b.provider ? esc(providerName(b.provider)) : "Todos"}</td>
        <td class="num">${usd.format(Number(b.spent))}</td><td class="num">${usd0.format(Number(b.monthly_limit))}</td>
        <td><span class="status">${ICONS[icon]}${label} (${Math.round(b.ratio * 100)}%)</span></td>
        <td><button class="btn small danger" data-act="del" type="button">Eliminar</button></td></tr>`; }).join("") || '<tr><td colspan="6" class="muted">Sin presupuestos</td></tr>'}
      </tbody></table></div></section>
    <section class="card"><div class="card-head"><h2>Nuevo presupuesto</h2></div>
      <datalist id="teamList2">${teams.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
      <form class="inline-form" id="budgetForm">
        <div class="field"><label for="b_team">Equipo</label><input id="b_team" name="team" list="teamList2" required></div>
        <div class="field"><label for="b_provider">Proveedor</label><select id="b_provider" name="provider"><option value="">Todos</option>${PROVIDER_ORDER.filter((p) => p !== "synthetic").map((p) => `<option value="${p}">${esc(providerName(p))}</option>`).join("")}</select></div>
        <div class="field"><label for="b_limit">Límite mensual (USD)</label><input id="b_limit" name="monthly_limit" type="number" min="1" step="0.01" required></div>
        <button class="btn primary" type="submit">Guardar</button>
      </form></section>`;
  panel.querySelectorAll('[data-act="del"]').forEach((b) => {
    b.onclick = async () => {
      const item = budgets.items[Number(b.closest("tr").dataset.i)];
      try { await api("/v1/budgets", { method: "DELETE", params: { team: item.team, provider: item.provider } }); toast("Presupuesto eliminado"); render(); }
      catch (err) { toast(err.message, true); }
    };
  });
  $("budgetForm").onsubmit = async (e) => {
    e.preventDefault();
    const data = Object.fromEntries(new FormData(e.currentTarget));
    try { await api("/v1/budgets", { method: "PUT", body: { team: data.team, provider: data.provider || null, monthly_limit: data.monthly_limit } }); toast("Presupuesto guardado"); render(); }
    catch (err) { toast(err.message, true); }
  };
}

function renderIngest(panel) {
  const base = state.config.apiBaseUrl || location.origin;
  const today = new Date().toISOString().slice(0, 10);
  const body = JSON.stringify({ records: [{ provider: "custom", service: "mi-llm-interno", principal: "user:ana", usage_date: today, input_tokens: 120000, output_tokens: 8000, requests: 42, cost: "1.35" }] });
  const auth = state.config.authMode === "local" ? `-H "x-tenant-id: ${state.config.tenant || "local"}"` : '-H "Authorization: Bearer $TOKEN"';
  panel.innerHTML = `
    <div class="panel-head"><h2>Ingesta por API</h2></div>
    <p class="muted">Cualquier sistema (un LLM propio, otra nube, una herramienta SaaS) puede reportar consumo con una llamada HTTP. Los envíos son idempotentes: reenviar el mismo registro no duplica el costo; enviar una versión corregida lo actualiza.</p>
    <section class="card" style="margin-bottom:12px"><div class="card-head"><h2>Endpoint</h2></div>
      <pre class="code">POST ${esc(base)}/v1/usage/records</pre>
      ${state.config.authMode === "local" ? "" : '<p class="muted">Autenticación: token OAuth 2.0 <em>client_credentials</em> con el scope <code>usagehub/ingest</code> (cliente M2M de Cognito).</p>'}</section>
    <section class="card" style="margin-bottom:12px"><div class="card-head"><h2>Ejemplo (bash / curl)</h2></div>
      <pre class="code">curl -X POST "${esc(base)}/v1/usage/records" \\
  -H "content-type: application/json" ${esc(auth)} \\
  -d '${esc(body)}'</pre></section>
    <section class="card"><div class="card-head"><h2>Campos</h2></div>
      <table class="data"><thead><tr><th>Campo</th><th>Descripción</th></tr></thead><tbody>
        <tr><td><code>service</code></td><td>Modelo o servicio consumido (obligatorio)</td></tr>
        <tr><td><code>principal</code></td><td>Quién lo consumió: usuario, API key, proyecto… (obligatorio). Asígnalo a una persona en <a href="#/configuracion/personas">Personas y equipos</a>.</td></tr>
        <tr><td><code>usage_date</code></td><td>Día del consumo, AAAA-MM-DD (obligatorio)</td></tr>
        <tr><td><code>input_tokens</code>, <code>output_tokens</code>, <code>cached_tokens</code>, <code>requests</code></td><td>Métricas enteras ≥ 0</td></tr>
        <tr><td><code>cost</code></td><td>Costo en USD como texto decimal, p. ej. <code>"12.50"</code></td></tr>
        <tr><td><code>provider</code></td><td>Opcional, por defecto <code>custom</code></td></tr>
      </tbody></table>
      <p class="muted" style="margin-bottom:0">Hasta 1.000 registros por petición. Contrato completo: <code>docs/openapi.yaml</code>.</p></section>`;
}

// Redibuja las gráficas cuando cambia la densidad de píxeles (zoom del navegador, mover la
// ventana a otro monitor). Si el contenedor no cambia de tamaño, Chart.js no se entera y el
// canvas queda escalado y borroso.
function watchPixelRatio() {
  const query = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
  query.addEventListener("change", () => {
    Object.values(state.charts).forEach((chart) => chart?.resize());
    watchPixelRatio();
  }, { once: true });
}

// ------------------------------------------------------------------ arranque
async function main() {
  state.config = await fetch("/config.json").then((r) => r.json()).catch(() => ({ apiBaseUrl: "", authMode: "local", tenant: "local" }));
  try { state.me = await api("/v1/me"); }
  catch (err) { $("banner").hidden = false; $("banner").textContent = `No se pudo conectar con la API: ${err.message}`; return; }
  if (!location.hash) history.replaceState(null, "", "#/real");
  document.querySelectorAll("[data-days]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll("[data-days]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    state.days = Number(btn.dataset.days); render();
  }));
  $("groupBy").addEventListener("change", (e) => { state.groupBy = e.target.value; render(); });
  document.querySelectorAll("[data-table-for]").forEach((btn) => btn.addEventListener("click", () => {
    const el = $(`${btn.dataset.tableFor}-table`); el.hidden = !el.hidden;
    btn.textContent = el.hidden ? "Ver tabla" : "Ocultar tabla";
  }));
  window.addEventListener("hashchange", render);
  watchPixelRatio();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  await render();
}

window.addEventListener("load", main);
