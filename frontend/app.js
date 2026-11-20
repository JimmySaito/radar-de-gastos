// Radar de gastos: SPA sin build step. Lee /config.json (generado por Terraform o el servidor local).
// Rutas: #/empresa/{real|ejemplo}            dashboard de consumo de la empresa
//        #/empresa/configuracion/{conexiones|licencias|personas|presupuestos|ingesta}
//        #/personal                         mis suscripciones (privado por persona)
//        #/personal/configuracion           administrar suscripciones y preferencias
// authMode "cognito": OAuth 2.0 Authorization Code + PKCE.  authMode "local": servidor local.

const PROVIDER_ORDER = ["anthropic", "openai", "google", "aws", "claude_code", "custom", "synthetic"];
const PROVIDER_LABEL = { anthropic: "Anthropic (Claude)", openai: "OpenAI", google: "Google (Gemini)", aws: "AWS", claude_code: "Claude Code (estimado)", custom: "Personalizado", synthetic: "Generador de prueba", subscription: "Suscripción" };
const state = { days: 30, groupBy: "provider", config: null, me: null, charts: {}, catalog: null, editing: null, lastView: "real", shown: null };
const TAB_LABEL = { conexiones: "Conexiones", licencias: "Licencias y suscripciones", personas: "Personas y equipos", presupuestos: "Presupuestos", ingesta: "Ingesta por API" };
const VIEW_LABEL = { real: "Datos reales", ejemplo: "Ejemplo" };

const $ = (id) => document.getElementById(id);
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const seriesColor = (i) => css(`--series-${(i % 8) + 1}`);
const providerColor = (p) => seriesColor(Math.max(PROVIDER_ORDER.indexOf(p), 0));
const usd = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("es", { notation: "compact", maximumFractionDigits: 1 });
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const providerName = (p) => PROVIDER_LABEL[p] || p;

// La CSP (style-src 'self') bloquea los atributos de estilo en línea del HTML generado, pero permite
// asignar estilos desde JavaScript. Colores y anchos dinámicos viajan en data-bg / data-w y se
// aplican aquí cada vez que se agrega contenido al documento.
function applyDynamicStyles(root) {
  const nodes = root.matches?.("[data-bg],[data-w]") ? [root] : [];
  nodes.push(...(root.querySelectorAll?.("[data-bg],[data-w]") ?? []));
  for (const el of nodes) {
    if (el.dataset.bg) el.style.background = el.dataset.bg;
    if (el.dataset.w) el.style.width = el.dataset.w;
  }
}
new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach((n) => n.nodeType === 1 && applyDynamicStyles(n))))
  .observe(document.documentElement, { childList: true, subtree: true });

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
const LEGACY = { real: "#/empresa/real", ejemplo: "#/empresa/ejemplo", configuracion: "#/empresa/configuracion" };

function route() {
  const parts = (location.hash || "#/empresa/real").split("/").slice(1);
  if (parts[0] === "personal") return { space: "personal", view: ["configuracion", "ejemplo"].includes(parts[1]) ? parts[1] : "real", tab: null };
  const view = ["real", "ejemplo", "configuracion"].includes(parts[1]) ? parts[1] : "real";
  return { space: "empresa", view, tab: parts[2] || "conexiones" };
}

function setCurrent(nodes, isActive) {
  nodes.forEach((n) => (isActive(n) ? n.setAttribute("aria-current", "page") : n.removeAttribute("aria-current")));
}

async function render() {
  // Enlaces anteriores (#/real, #/configuracion/...) siguen funcionando.
  const first = (location.hash || "").split("/")[1];
  if (LEGACY[first]) { location.replace(LEGACY[first] + location.hash.slice(2 + first.length)); return; }

  const { space, view, tab } = route();
  const empresa = space === "empresa";
  const isSettings = view === "configuracion";
  const isDemo = view === "ejemplo";
  setCurrent(document.querySelectorAll("[data-space]"), (a) => a.dataset.space === space);
  setCurrent(document.querySelectorAll("[data-pview]"), (a) => !empresa && a.dataset.pview === view);
  setCurrent(document.querySelectorAll("[data-view]"), (a) => empresa && !isSettings && a.dataset.view === view);
  $("views").hidden = !empresa;
  $("filters").hidden = !empresa || isSettings;
  $("footer").hidden = !empresa;
  $("dashboard").hidden = !empresa || isSettings;
  $("settings").hidden = !empresa || !isSettings;
  $("personal").hidden = empresa || isSettings;
  $("personalSettings").hidden = empresa || !isSettings;
  $("banner").hidden = true;

  if (empresa && !isSettings) state.lastView = view;
  const back = `#/empresa/${state.lastView}`;
  const gear = $("settingsLink");
  // El engranaje es un interruptor y depende del espacio: abre su configuración y, estando dentro, vuelve.
  if (empresa) {
    gear.hidden = !state.me?.is_admin;
    gear.href = isSettings ? back : `#/empresa/configuracion/${state.lastTab || "conexiones"}`;
  } else {
    gear.hidden = false;
    gear.href = isSettings ? "#/personal" : "#/personal/configuracion";
  }
  gear.setAttribute("aria-label", isSettings ? "Cerrar configuración" : (empresa ? "Configuración de la empresa" : "Configuración personal"));
  gear.title = gear.getAttribute("aria-label");
  if (isSettings) gear.setAttribute("aria-current", "page"); else gear.removeAttribute("aria-current");

  $("backBtn").href = back;
  $("crumbView").href = back;
  $("crumbView").textContent = VIEW_LABEL[state.lastView];
  $("backLabel").textContent = `Volver a ${VIEW_LABEL[state.lastView]}`;
  if (empresa && isSettings) { state.lastTab = tab; $("crumbTab").textContent = TAB_LABEL[tab] || tab; }
  document.title = !empresa
    ? `${isSettings ? "Configuración · " : isDemo ? "Ejemplo · " : ""}Personal · Radar de gastos`
    : isSettings ? `${TAB_LABEL[tab] || "Configuración"} · Empresa · Radar de gastos` : `${VIEW_LABEL[state.lastView]} · Empresa · Radar de gastos`;

  if (!empresa) await (isSettings ? renderPersonalSettings() : renderPersonal(isDemo));
  else if (isSettings) await renderSettings(tab);
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
    // Sobremuestreo: con zoom del navegador ≠ 100 % la densidad es fraccionaria (p. ej. 0,9) y el
    // texto del canvas se ve borroso. Dibujar al menos a 2× lo deja nítido con cualquier zoom.
    devicePixelRatio: chartPixelRatio(),
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
    <tr><td><span class="swatch" data-bg="${providerColor(p)}"></span>${esc(providerName(p))}</td>
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
    await renderLicensesCard(isDemo);
    state.shown = { days: state.days, isDemo, at: new Date() };
    $("dashContent").classList.remove("stale");
  } catch (err) {
    $("banner").hidden = false;
    const shown = state.shown;
    if (shown && shown.isDemo === isDemo) {
      // Se conservan los últimos datos, pero sin fingir que son del rango pedido.
      state.days = shown.days;
      syncRangeButtons();
      $("dashContent").classList.add("stale");
      const at = shown.at.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" });
      $("banner").textContent = `No se pudieron actualizar los datos: ${err.message.replace(/\.?$/, ".")} Se muestran los últimos ${shown.days} días consultados a las ${at}.`;
    } else {
      $("banner").textContent = `No se pudieron cargar los datos: ${err.message}`;
    }
  }
}

function syncRangeButtons() {
  document.querySelectorAll("[data-days]").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === state.days)));
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
    else if (tab === "licencias") await renderRecurringAdmin(panel, "business");
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
      ${list.length ? "" : '<div class="card"><p class="muted m-0">Todavía no hay conexiones. Agrega una para empezar a ver tus datos reales.</p></div>'}
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
  $("connForm").innerHTML = `<section class="card mb-12">
    <div class="card-head"><h2>¿Qué quieres conectar?</h2><button class="link" type="button" id="cancelPick">Cancelar</button></div>
    <div class="providers">${specs.map((s) => `<button type="button" class="provider-option" data-provider="${esc(s.provider)}"><strong>${esc(s.name)}</strong><span>${esc(s.description)}</span></button>`).join("")}</div>
    <p class="muted m-0 mt-12">¿Otro sistema? Envíalo por API en <a href="#/empresa/configuracion/ingesta">Ingesta por API</a>.</p>
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
  $("connForm").innerHTML = `<section class="card mb-12">
    <div class="card-head"><h2>${conn ? "Editar" : "Nueva"} conexión · ${esc(spec.name)}</h2><button class="link" type="button" id="cancelForm">Cancelar</button></div>
    <form class="form" id="connEdit" novalidate>
      <p class="muted m-0">${esc(spec.description)} ${spec.notes ? esc(spec.notes) : ""} ${spec.docs_url ? `<a class="link" href="${esc(spec.docs_url)}" target="_blank" rel="noopener noreferrer">Documentación ↗</a>` : ""}</p>
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
      <p class="muted m-0 fs-12">Las credenciales se guardan cifradas en el servidor (Secrets Manager en AWS, <code>~/.radar-gastos</code> en local) y nunca se vuelven a mostrar.</p>
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
    <section class="card mb-12">
      <div class="card-head"><h2>Identidades sin asignar (${unmapped.length})</h2></div>
      <div class="table-scroll"><table class="data"><thead><tr><th>Proveedor</th><th>Identidad</th><th class="num">Costo 90 d</th><th>Persona</th><th>Equipo</th><th></th></tr></thead><tbody>
      ${unmapped.map((r, i) => `<tr data-i="${i}"><td>${esc(providerName(r.provider))}</td><td><code>${esc(r.name)}</code></td><td class="num">${usd.format(Number(r.cost))}</td>
        <td><input data-k="member" placeholder="nombre@empresa.com"></td><td><input data-k="team" list="teamList" placeholder="equipo"></td>
        <td><button class="btn small primary" data-act="assign" type="button">Asignar</button></td></tr>`).join("") || '<tr><td colspan="6" class="muted">Todo el consumo está asignado 🎉</td></tr>'}
      </tbody></table></div>
    </section>
    <section class="card mb-12">
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
    <section class="card mb-12"><div class="table-scroll"><table class="data">
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
    <section class="card mb-12"><div class="card-head"><h2>Endpoint</h2></div>
      <pre class="code">POST ${esc(base)}/v1/usage/records</pre>
      ${state.config.authMode === "local" ? "" : '<p class="muted">Autenticación: token OAuth 2.0 <em>client_credentials</em> con el scope <code>radar-gastos/ingest</code> (cliente M2M de Cognito).</p>'}</section>
    <section class="card mb-12"><div class="card-head"><h2>Ejemplo (bash / curl)</h2></div>
      <pre class="code">curl -X POST "${esc(base)}/v1/usage/records" \\
  -H "content-type: application/json" ${esc(auth)} \\
  -d '${esc(body)}'</pre></section>
    <section class="card"><div class="card-head"><h2>Campos</h2></div>
      <table class="data"><thead><tr><th>Campo</th><th>Descripción</th></tr></thead><tbody>
        <tr><td><code>service</code></td><td>Modelo o servicio consumido (obligatorio)</td></tr>
        <tr><td><code>principal</code></td><td>Quién lo consumió: usuario, API key, proyecto… (obligatorio). Asígnalo a una persona en <a href="#/empresa/configuracion/personas">Personas y equipos</a>.</td></tr>
        <tr><td><code>usage_date</code></td><td>Día del consumo, AAAA-MM-DD (obligatorio)</td></tr>
        <tr><td><code>input_tokens</code>, <code>output_tokens</code>, <code>cached_tokens</code>, <code>requests</code></td><td>Métricas enteras ≥ 0</td></tr>
        <tr><td><code>cost</code></td><td>Costo en USD como texto decimal, p. ej. <code>"12.50"</code></td></tr>
        <tr><td><code>provider</code></td><td>Opcional, por defecto <code>custom</code></td></tr>
      </tbody></table>
      <p class="muted mb-0">Hasta 1.000 registros por petición. Contrato completo: <code>docs/openapi.yaml</code>.</p></section>`;
}

// ------------------------------------------------------------------ gastos recurrentes
// Personal: suscripciones de cada persona (streaming, música, IA, software...).
// Empresa: licencias SaaS del tenant. Los montos se muestran en su moneda, sin conversión.
const RECUR = { catalogs: {}, editing: null, scope: "personal", profiles: [], member: { real: null, ejemplo: null } };
const personColor = (i) => seriesColor(i);
const initials = (name) => (name === "Sin asignar" ? "?" : name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase());
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const CURRENCIES = ["USD", "COP", "MXN", "EUR", "ARS", "CLP", "PEN", "BRL", "GBP", "CAD"];
const ZERO_DECIMALS = new Set(["COP", "CLP", "JPY", "KRW", "PYG"]);
const CYCLE_LABEL = { weekly: "Semanal", monthly: "Mensual", quarterly: "Trimestral", annual: "Anual" };
const QUICK_ADD = ["netflix", "spotify", "max", "disney", "youtube", "prime", "chatgpt", "claude", "icloud", "gym"];

function money(amount, currency) {
  const digits = ZERO_DECIMALS.has(currency) ? 0 : 2;
  return new Intl.NumberFormat("es", { style: "currency", currency, minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(amount));
}
const moneyLines = (byCurrency) => Object.entries(byCurrency || {}).map(([c, v]) => money(v, c));
const moneyText = (byCurrency) => moneyLines(byCurrency).join(" · ") || money(0, prefCurrency());

function prefCurrency() { try { return localStorage.getItem("radar.currency") || localStorage.getItem("uh.currency") || "USD"; } catch { return "USD"; } }
function setPrefCurrency(c) { try { localStorage.setItem("radar.currency", c); } catch { /* sin almacenamiento */ } }
const todayISO = () => { const d = new Date(); return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10); };
const dueLabel = (n) => (n === 0 ? "hoy" : n === 1 ? "mañana" : `en ${n} días`);
const shortDate = (iso, opts = { day: "numeric", month: "short" }) => new Date(`${iso}T12:00:00`).toLocaleDateString("es", opts);

async function recurringCatalog(scope) {
  RECUR.catalogs[scope] ??= await api(`/v1/recurring/${scope}/catalog`);
  return RECUR.catalogs[scope];
}
const categoryLabel = (cat, key) => cat.categories.find((c) => c.key === key)?.label || key;
const categoryColor = (cat, key) => seriesColor(Math.max(cat.categories.findIndex((c) => c.key === key), 0));

function chargeItem(c, cat) {
  return `<li class="charge${c.soon ? " soon" : ""}">
    <div class="date-badge"><b>${new Date(`${c.date}T12:00:00`).getDate()}</b><span>${shortDate(c.date, { month: "short" })}</span></div>
    <div class="charge-main"><strong>${esc(c.name)}</strong>
      <span><span class="swatch" data-bg="${categoryColor(cat, c.category)}"></span>${esc(categoryLabel(cat, c.category))}</span></div>
    <div class="charge-side"><span class="amt">${money(c.amount, c.currency)}</span>
      <span class="due">${c.soon ? '<svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l7 13H1z" fill="var(--status-warning)"/></svg>' : ""}${dueLabel(c.days_left)}</span></div>
  </li>`;
}

function calendarGrid(summary, cat) {
  const start = new Date(`${summary.this_month.from}T12:00:00`);
  const daysInMonth = new Date(start.getFullYear(), start.getMonth() + 1, 0).getDate();
  const lead = (start.getDay() + 6) % 7; // lunes primero
  const byDay = {};
  for (const c of summary.this_month.calendar) (byDay[Number(c.date.slice(8, 10))] ??= []).push(c);
  const today = Number(summary.today.slice(8, 10));
  const cells = [];
  for (let i = 0; i < lead; i++) cells.push('<div class="cal-cell empty" aria-hidden="true"></div>');
  for (let d = 1; d <= daysInMonth; d++) {
    const list = byDay[d] || [];
    const title = list.map((c) => `${c.name}: ${money(c.amount, c.currency)}`).join("\n");
    cells.push(`<div class="cal-cell${d === today ? " today" : ""}${list.length ? " has" : ""}${d < today ? " past" : ""}" ${title ? `title="${esc(title)}"` : ""}>
      <span class="cal-day">${d}</span>
      <span class="cal-dots">${list.slice(0, 4).map((c) => `<i data-bg="${categoryColor(cat, c.category)}"></i>`).join("")}</span>
      ${list.length ? `<span class="sr-only">${esc(title)}</span>` : ""}
    </div>`);
  }
  const month = start.toLocaleDateString("es", { month: "long", year: "numeric" });
  return `<div class="cal-head"><h2>Calendario de ${esc(month)}</h2></div>
    <div class="cal-grid" role="grid">${["L", "M", "X", "J", "V", "S", "D"].map((w) => `<div class="cal-week" role="columnheader">${w}</div>`).join("")}${cells.join("")}</div>`;
}

function categoryChart(id, summary, cat) {
  const currencies = [...new Set(summary.by_category.map((r) => r.currency))];
  const currency = currencies.includes(prefCurrency()) ? prefCurrency() : currencies[0];
  const rows = summary.by_category.filter((r) => r.currency === currency);
  const opts = baseOptions();
  draw(id, {
    type: "bar",
    data: { labels: rows.map((r) => categoryLabel(cat, r.category)), datasets: [{
      label: `Equivalente mensual (${currency})`, data: rows.map((r) => Number(r.monthly)),
      backgroundColor: rows.map((r) => categoryColor(cat, r.category)), borderRadius: 4, borderSkipped: "left", maxBarThickness: 22 }] },
    options: { ...opts, indexAxis: "y", plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => money(c.parsed.x, currency) } } },
      scales: { x: { grid: { color: css("--grid") }, ticks: { color: css("--text-muted"), callback: (v) => money(v, currency) }, border: { display: false } },
                y: { grid: { display: false }, ticks: { color: css("--text-secondary") }, border: { color: css("--border") } } } },
  });
  return currencies.length > 1 ? `Mostrando ${currency}. También tienes gastos en ${currencies.filter((c) => c !== currency).join(", ")}.` : "";
}

function whoCell(e) {
  if (!e.member_names?.length) return '<span class="muted">Sin asignar</span>';
  return `${e.member_names.map((n) => esc(n)).join(", ")}${e.member_names.length > 1 ? ` <span class="chip">Compartida</span>` : ""}`;
}

function expenseTable(items, cat, { business = false, actions = true, people = false, partial = false } = {}) {
  if (!items.length) return '<p class="muted">Sin registros todavía.</p>';
  return `<div class="table-scroll"><table class="data"><thead><tr>
      <th>Servicio</th>${people ? "<th>De quién</th>" : ""}<th>Categoría</th><th>Ciclo</th>${business ? '<th class="num">Usuarios</th>' : ""}<th class="num">${partial ? "Su parte" : "Cobro"}</th><th class="num">Equivalente mensual</th><th>Próximo cobro</th>${actions ? "<th></th>" : ""}
    </tr></thead><tbody>
    ${items.map((e) => `<tr data-id="${esc(e.expense_id)}" class="${e.active ? "" : "inactive"}">
      <td><strong>${esc(e.name)}</strong>${e.plan ? ` <span class="muted">· ${esc(e.plan)}</span>` : ""}${e.active ? "" : ' <span class="chip">Cancelada</span>'}${business && e.owner ? `<div class="muted small">${esc(e.owner)}${e.team ? ` · ${esc(e.team)}` : ""}</div>` : ""}</td>
      ${people ? `<td>${whoCell(e)}</td>` : ""}
      <td><span class="swatch" data-bg="${categoryColor(cat, e.category)}"></span>${esc(categoryLabel(cat, e.category))}</td>
      <td>${CYCLE_LABEL[e.cycle]}</td>
      ${business ? `<td class="num">${e.seats}</td>` : ""}
      <td class="num">${money(e.charge, e.currency)}${partial && e.charge !== e.full_charge ? `<div class="muted small">de ${money(e.full_charge, e.currency)}</div>` : ""}</td>
      <td class="num">${money(e.monthly_equivalent, e.currency)}</td>
      <td>${e.next_charge ? `${shortDate(e.next_charge)} <span class="muted">(${dueLabel(e.days_left)})</span>` : "—"}</td>
      ${actions ? '<td class="row-actions"><button class="btn small" data-act="edit" type="button">Editar</button></td>' : ""}
    </tr>`).join("")}</tbody></table></div>`;
}

function bindEdit(container, items, scope) {
  container.querySelectorAll('[data-act="edit"]').forEach((b) => {
    b.onclick = () => openExpenseDialog(scope, items.find((e) => e.expense_id === b.closest("tr").dataset.id));
  });
}

function personFilter(data, isDemo) {
  const el = $("personFilter");
  const people = data.by_member;
  el.hidden = !data.profiles.length;
  if (el.hidden) return;
  const key = isDemo ? "ejemplo" : "real";
  const current = RECUR.member[key];
  const chip = (id, label, color) => `<button type="button" class="person-chip" data-member="${esc(id ?? "")}" aria-pressed="${String((current ?? null) === id)}">
    ${color ? `<span class="avatar xs" data-bg="${color}">${esc(initials(label))}</span>` : ""}${esc(label)}</button>`;
  el.innerHTML = `<span class="muted small">Ver:</span>${chip(null, "Todos")}${people.map((p, i) => chip(p.member_id, p.name, p.member_id === "sin-asignar" ? css("--text-muted") : personColor(i))).join("")}`;
  el.querySelectorAll("[data-member]").forEach((b) => {
    b.onclick = () => { RECUR.member[key] = b.dataset.member || null; render(); };
  });
}

function peopleCard(data) {
  const people = data.by_member;
  if (!people.length) return "";
  const main = prefCurrency();
  const currencies = [...new Set(people.flatMap((p) => Object.keys(p.monthly)))];
  const cur = currencies.includes(main) ? main : currencies[0];
  const max = Math.max(...people.map((p) => Number(p.monthly[cur] || 0)), 1);
  return `<section class="card wide-2">
    <div class="card-head"><h2>Gasto por persona</h2><span class="muted small">Equivalente mensual · lo compartido se divide en partes iguales</span></div>
    <div class="people">${people.map((p, i) => {
      const color = p.member_id === "sin-asignar" ? css("--text-muted") : personColor(i);
      const value = Number(p.monthly[cur] || 0);
      return `<button type="button" class="person-row" data-member="${esc(p.member_id)}" title="Ver solo ${esc(p.name)}">
        <span class="avatar" data-bg="${color}">${esc(initials(p.name))}</span>
        <span class="person-name"><strong>${esc(p.name)}</strong><span class="muted small">${p.relation ? `${esc(p.relation)} · ` : ""}${plural(p.count, "suscripción", "suscripciones")}${p.shared ? ` (${plural(p.shared, "compartida", "compartidas")})` : ""}</span></span>
        <span class="person-bar" aria-hidden="true"><i data-w="${(100 * value / max).toFixed(1)}%" data-bg="${color}"></i></span>
        <span class="person-amount">${moneyLines(p.monthly).map((l) => `<span>${l}</span>`).join("") || "—"}</span>
      </button>`;
    }).join("")}</div>
  </section>`;
}

async function renderPersonal(isDemo = false) {
  const body = $("personalBody");
  const key = isDemo ? "ejemplo" : "real";
  $("personalDemoNotice").hidden = !isDemo;
  $("personalAdd").hidden = isDemo;
  body.innerHTML = '<p class="muted">Cargando…</p>';
  try {
    const params = { dataset: isDemo ? "demo" : undefined, member: RECUR.member[key] || undefined };
    let data;
    try { data = await api("/v1/recurring/personal", { params }); }
    catch (e) { if (!params.member) throw e; RECUR.member[key] = null; data = await api("/v1/recurring/personal", { params: { ...params, member: undefined } }); }
    const cat = await recurringCatalog("personal");
    if (!isDemo) RECUR.profiles = data.profiles;
    personFilter(data, isDemo);
    const { items, summary } = data;
    const who = data.member ? data.by_member.find((p) => p.member_id === data.member)?.name : null;
    $("personalTitle").textContent = who ? `Suscripciones de ${who}` : (isDemo ? "Suscripciones del hogar (ejemplo)" : "Mis suscripciones");

    if (!items.length && !data.member) {
      const quick = cat.services.filter((s) => QUICK_ADD.includes(s.key));
      body.innerHTML = `<div class="empty">
        <h1>Lleva el control de tus suscripciones</h1>
        <p>Agrega lo que pagas cada mes o cada año, con su fecha de cobro, y verás cuánto gastas, qué se cobra pronto y cuánto suma al año. Puedes crear personas (por ejemplo, tú y tu familia) para ver el gasto de cada una.</p>
        <div class="quick-add">${quick.map((s) => `<button type="button" class="btn" data-quick="${esc(s.key)}">+ ${esc(s.name)}</button>`).join("")}</div>
        <div class="row"><button type="button" class="btn primary" data-add-expense="personal">Agregar otra suscripción</button>
          <a class="btn" href="#/personal/configuracion">Crear personas</a>
          <a class="btn" href="#/personal/ejemplo">Ver el ejemplo</a></div>
      </div>`;
      body.querySelectorAll("[data-quick]").forEach((b) => { b.onclick = () => openExpenseDialog("personal", null, b.dataset.quick); });
      body.querySelector("[data-add-expense]").onclick = () => openExpenseDialog("personal");
      return;
    }
    const next = summary.next_charge;
    const multi = (v) => moneyLines(v).map((l) => `<span class="money-line">${l}</span>`).join("") || money(0, prefCurrency());
    const share = who ? " (su parte)" : "";
    body.innerHTML = `
      <section class="kpis" aria-label="Resumen">
        <article class="kpi"><h2>Gasto mensual${share}</h2><p class="value stack-lines">${multi(summary.monthly_equivalent)}</p><p class="sub">${summary.active_count} suscripciones activas · equivalente mensual</p></article>
        <article class="kpi"><h2>Cobros de este mes${share}</h2><p class="value stack-lines">${multi(summary.this_month.total)}</p><p class="sub">${summary.this_month.charges} cobros · quedan ${moneyText(summary.this_month.remaining)}</p></article>
        <article class="kpi"><h2>Próximo cobro</h2><p class="value">${next ? esc(next.name) : "—"}</p><p class="sub">${next ? `${money(next.amount, next.currency)} · ${shortDate(next.date)} (${dueLabel(next.days_left)})` : "Sin cobros programados"}</p></article>
        <article class="kpi"><h2>Proyección anual${share}</h2><p class="value stack-lines">${multi(summary.annual_projection)}</p><p class="sub">si se mantienen las suscripciones actuales</p></article>
      </section>
      <div class="grid">
        ${data.member ? "" : peopleCard(data)}
        <section class="card">
          <div class="card-head"><h2>Próximos cobros (31 días)</h2></div>
          ${summary.upcoming.length ? `<ul class="charges">${summary.upcoming.slice(0, 8).map((c) => chargeItem(c, cat)).join("")}</ul>` : '<p class="muted">No hay cobros en los próximos 31 días.</p>'}
        </section>
        <section class="card">${calendarGrid(summary, cat)}</section>
        <section class="card wide-2">
          <div class="card-head"><h2>Gasto mensual por categoría</h2></div>
          <div class="chart-box short"><canvas id="categoryChart" role="img" aria-label="Gasto mensual por categoría"></canvas></div>
          <p class="muted small" id="categoryNote"></p>
        </section>
        <section class="card wide-2">
          <div class="card-head"><h2>${who ? `Suscripciones de ${esc(who)}` : "Todas las suscripciones"} (${items.length})</h2>${isDemo ? "" : '<a class="link" href="#/personal/configuracion">Administrar</a>'}</div>
          <div id="personalTable">${expenseTable(items, cat, { actions: !isDemo, people: Boolean(data.profiles.length), partial: Boolean(who) })}</div>
        </section>
      </div>`;
    if (summary.by_category.length) $("categoryNote").textContent = categoryChart("categoryChart", summary, cat);
    body.querySelectorAll(".person-row").forEach((b) => { b.onclick = () => { RECUR.member[key] = b.dataset.member; render(); window.scrollTo({ top: 0, behavior: "smooth" }); }; });
    if (!isDemo) bindEdit($("personalTable"), items, "personal");
  } catch (err) {
    body.innerHTML = `<p class="banner error">No se pudieron cargar las suscripciones: ${esc(err.message)}</p>`;
  }
}

async function renderPersonalSettings() {
  const body = $("personalSettingsBody");
  body.innerHTML = '<p class="muted">Cargando…</p>';
  try {
    const [data, cat] = await Promise.all([api("/v1/recurring/personal"), recurringCatalog("personal")]);
    RECUR.profiles = data.profiles;
    const stats = Object.fromEntries(data.by_member.map((p) => [p.member_id, p]));
    body.innerHTML = `
      <section class="card mb-12">
        <div class="panel-head"><h2>Personas (${data.profiles.length})</h2></div>
        <p class="muted">Crea un perfil por persona (por ejemplo, tú, tu pareja o tus hijos) y asígnale sus suscripciones. Un plan compartido entre varias personas se divide en partes iguales en el panel.</p>
        <div class="people-admin">${data.profiles.map((p, i) => `<div class="person-admin" data-id="${esc(p.profile_id)}">
            <span class="avatar" data-bg="${personColor(i)}">${esc(initials(p.name))}</span>
            <input aria-label="Nombre" value="${esc(p.name)}" maxlength="60" data-k="name">
            <input aria-label="Relación" value="${esc(p.relation)}" maxlength="40" data-k="relation" placeholder="Relación (opcional)">
            <span class="muted small">${plural(stats[p.profile_id]?.count || 0, "suscripción", "suscripciones")} · ${moneyText(stats[p.profile_id]?.monthly)}/mes</span>
            <button class="btn small" data-act="save" type="button">Guardar</button>
            <button class="btn small danger" data-act="del" type="button">Eliminar</button>
          </div>`).join("") || '<p class="muted">Todavía no hay personas.</p>'}</div>
        <form class="inline-form mt-12" id="newPerson">
          <div class="field"><label for="np_name">Nombre</label><input id="np_name" name="name" maxlength="60" required placeholder="Ej. Jimmy"></div>
          <div class="field"><label for="np_rel">Relación <span class="muted">(opcional)</span></label><input id="np_rel" name="relation" maxlength="40" placeholder="Yo, pareja, hija…"></div>
          <button class="btn primary" type="submit">+ Agregar persona</button>
        </form>
      </section>
      <section class="card mb-12">
        <div class="panel-head"><h2>Suscripciones (${data.items.length})</h2>
          <div class="row"><button class="btn" type="button" id="exportCsv" ${data.items.length ? "" : "disabled"}>Exportar CSV</button>
          <button class="btn primary" type="button" data-add-expense="personal">+ Agregar suscripción</button></div></div>
        <p class="muted">Edita una suscripción para cambiar su precio, fecha de cobro o a quién pertenece. Si la cancelas, márcala como inactiva o indica desde cuándo: se conserva en el historial y deja de contar.</p>
        <div id="personalAdminTable">${expenseTable(data.items, cat, { people: Boolean(data.profiles.length) })}</div>
      </section>
      <section class="card">
        <div class="card-head"><h2>Preferencias</h2></div>
        <form class="inline-form" id="prefsForm">
          <div class="field"><label for="prefCurrency">Moneda predeterminada</label>
            <select id="prefCurrency">${CURRENCIES.map((c) => `<option value="${c}" ${c === prefCurrency() ? "selected" : ""}>${c}</option>`).join("")}</select>
            <div class="help">Se propone al agregar suscripciones y se usa en las gráficas. Se guarda en este navegador.</div></div>
        </form>
      </section>`;
    bindEdit($("personalAdminTable"), data.items, "personal");
    body.querySelector("[data-add-expense]").onclick = () => openExpenseDialog("personal");
    $("prefCurrency").onchange = (e) => { setPrefCurrency(e.target.value); toast(`Moneda predeterminada: ${e.target.value}`); };
    $("exportCsv").onclick = () => exportCsv(data.items, cat);
    $("newPerson").onsubmit = async (e) => {
      e.preventDefault();
      const f = Object.fromEntries(new FormData(e.currentTarget));
      try { await api("/v1/personal/profiles", { method: "PUT", body: { name: f.name, relation: f.relation } }); toast(`${f.name} agregado`); render(); }
      catch (err) { toast(err.message, true); }
    };
    body.querySelectorAll(".person-admin").forEach((row) => {
      const id = row.dataset.id;
      row.querySelector('[data-act="save"]').onclick = async () => {
        const name = row.querySelector('[data-k="name"]').value; const relation = row.querySelector('[data-k="relation"]').value;
        try { await api("/v1/personal/profiles", { method: "PUT", body: { profile_id: id, name, relation } }); toast("Persona actualizada"); render(); }
        catch (err) { toast(err.message, true); }
      };
      row.querySelector('[data-act="del"]').onclick = async () => {
        const name = row.querySelector('[data-k="name"]').value;
        if (!confirm(`¿Eliminar a ${name}? Sus suscripciones se conservan: pasan a "sin asignar" o quedan solo con las demás personas con quienes las compartía.`)) return;
        try { const r = await api(`/v1/personal/profiles/${encodeURIComponent(id)}`, { method: "DELETE" }); toast(`${name} eliminado (${r.expenses_updated} suscripciones actualizadas)`); if (RECUR.member.real === id) RECUR.member.real = null; render(); }
        catch (err) { toast(err.message, true); }
      };
    });
  } catch (err) {
    body.innerHTML = `<p class="banner error">No se pudo cargar: ${esc(err.message)}</p>`;
  }
}

function exportCsv(items, cat) {
  const head = ["servicio", "plan", "categoria", "ciclo", "monto", "moneda", "equivalente_mensual", "proximo_cobro", "activa"];
  const quote = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const lines = items.map((e) => [e.name, e.plan, categoryLabel(cat, e.category), CYCLE_LABEL[e.cycle], e.charge, e.currency, e.monthly_equivalent, e.next_charge || "", e.active ? "sí" : "no"].map(quote).join(","));
  const blob = new Blob([`﻿${[head.join(","), ...lines].join("\n")}`], { type: "text/csv;charset=utf-8" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: `suscripciones-${todayISO()}.csv` });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

async function renderRecurringAdmin(panel, scope) {
  const [data, cat] = await Promise.all([api(`/v1/recurring/${scope}`), recurringCatalog(scope)]);
  const s = data.summary;
  panel.innerHTML = `
    <div class="panel-head"><h2>Licencias y suscripciones</h2><button class="btn primary" type="button" id="addLicense">+ Agregar licencia</button></div>
    <p class="muted">Herramientas que la empresa paga por usuario o con tarifa fija (Slack, GitHub, Microsoft 365, Claude Team…). Se suman al gasto mensual de la empresa y avisan de las próximas renovaciones.</p>
    ${data.items.length ? `<div class="kpis compact">
      <article class="kpi"><h2>Gasto mensual</h2><p class="value stack-lines">${moneyLines(s.monthly_equivalent).map((l) => `<span class="money-line">${l}</span>`).join("")}</p><p class="sub">${s.active_count} activas</p></article>
      <article class="kpi"><h2>Proyección anual</h2><p class="value stack-lines">${moneyLines(s.annual_projection).map((l) => `<span class="money-line">${l}</span>`).join("")}</p></article>
      <article class="kpi"><h2>Próxima renovación</h2><p class="value">${s.next_charge ? esc(s.next_charge.name) : "—"}</p><p class="sub">${s.next_charge ? `${money(s.next_charge.amount, s.next_charge.currency)} · ${shortDate(s.next_charge.date)} (${dueLabel(s.next_charge.days_left)})` : ""}</p></article>
    </div>` : ""}
    <section class="card"><div id="licensesTable">${expenseTable(data.items, cat, { business: true })}</div></section>`;
  $("addLicense").onclick = () => openExpenseDialog(scope);
  bindEdit($("licensesTable"), data.items, scope);
}

async function renderLicensesCard(isDemo) {
  const card = $("licensesCard");
  if (isDemo) { card.hidden = true; return; }
  try {
    const { items, summary } = await api("/v1/recurring/business");
    card.hidden = !items.length && !state.me?.is_admin;
    const next = summary.next_charge;
    $("licensesSummary").innerHTML = items.length
      ? `<div class="license-summary">
          <div><span class="muted small">Gasto mensual</span><strong>${moneyText(summary.monthly_equivalent)}</strong></div>
          <div><span class="muted small">Proyección anual</span><strong>${moneyText(summary.annual_projection)}</strong></div>
          <div><span class="muted small">Próxima renovación</span><strong>${next ? `${esc(next.name)} · ${shortDate(next.date)}` : "—"}</strong>${next ? `<span class="muted small">${money(next.amount, next.currency)} · ${dueLabel(next.days_left)}</span>` : ""}</div>
        </div>`
      : '<p class="muted m-0">Registra las licencias SaaS de la empresa (Slack, GitHub, Microsoft 365, Claude Team…) para ver su costo mensual junto al consumo.</p>';
  } catch {
    card.hidden = true;
  }
}

// ---------------- diálogo de alta / edición
function setupExpenseDialog() {
  const dialog = $("expenseDialog");
  dialog.querySelectorAll("[data-close-dialog]").forEach((b) => { b.onclick = () => dialog.close(); });
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); }); // clic fuera del cuadro
  document.querySelectorAll("[data-add-expense]").forEach((b) => { b.onclick = () => openExpenseDialog(b.dataset.addExpense); });
  $("expenseForm").onsubmit = saveExpense;
  $("expenseDelete").onclick = deleteExpense;
}

function memberOptions(selected) {
  if (!RECUR.profiles.length) return '<p class="muted small m-0">Aún no hay personas. Crea una aquí o en Configuración personal.</p>';
  return RECUR.profiles.map((p, i) => `<label class="member-option"><input type="checkbox" value="${esc(p.profile_id)}" ${selected.includes(p.profile_id) ? "checked" : ""}>
    <span class="avatar xs" data-bg="${personColor(i)}">${esc(initials(p.name))}</span>${esc(p.name)}</label>`).join("");
}

async function openExpenseDialog(scope, expense = null, presetService = null) {
  if (scope === "personal" && !RECUR.profiles.length) {
    try { RECUR.profiles = (await api("/v1/personal/profiles")).items; } catch { /* se puede crear en el formulario */ }
  }
  const cat = await recurringCatalog(scope);
  const business = scope === "business";
  RECUR.scope = scope;
  RECUR.editing = expense;
  const preset = cat.services.find((s) => s.key === (expense?.service || presetService)) || null;
  const v = expense || {
    name: preset && preset.key !== "other" ? preset.name : "", service: preset?.key || "other", category: preset?.category || "otros",
    plan: "", amount: "", currency: prefCurrency(), cycle: preset?.cycle || "monthly", billing_date: todayISO(),
    seats: 1, owner: "", team: "", active: true, start_date: "", end_date: "", notes: "",
    member_ids: RECUR.member.real && RECUR.member.real !== "sin-asignar" ? [RECUR.member.real] : [],
  };
  const currencies = CURRENCIES.includes(v.currency) ? CURRENCIES : [v.currency, ...CURRENCIES];
  const plans = cat.services.find((s) => s.key === v.service)?.plans || [];
  $("expenseDialogTitle").textContent = expense ? `Editar ${expense.name}` : (business ? "Nueva licencia" : "Nueva suscripción");
  $("expenseDelete").hidden = !expense;
  $("expenseFields").innerHTML = `
    <div class="fields-2">
      <div class="field"><label for="x_service">Servicio</label>
        <select id="x_service" name="service">${cat.services.map((s) => `<option value="${esc(s.key)}" ${s.key === v.service ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
      <div class="field"><label for="x_name">Nombre</label><input id="x_name" name="name" required maxlength="120" value="${esc(v.name)}" placeholder="Ej. Netflix"></div>
    </div>
    <div class="fields-2">
      <div class="field"><label for="x_plan">Plan <span class="muted">(opcional)</span></label><input id="x_plan" name="plan" list="x_plans" maxlength="120" value="${esc(v.plan)}"><datalist id="x_plans">${plans.map((p) => `<option value="${esc(p)}">`).join("")}</datalist></div>
      <div class="field"><label for="x_category">Categoría</label>
        <select id="x_category" name="category">${cat.categories.map((c) => `<option value="${esc(c.key)}" ${c.key === v.category ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select></div>
    </div>
    <div class="fields-3">
      <div class="field"><label for="x_amount">${business ? "Precio por usuario" : "Costo"}</label><input id="x_amount" name="amount" type="number" inputmode="decimal" min="0.01" step="0.01" required value="${esc(v.amount)}"></div>
      <div class="field"><label for="x_currency">Moneda</label><select id="x_currency" name="currency">${currencies.map((c) => `<option ${c === v.currency ? "selected" : ""}>${c}</option>`).join("")}</select></div>
      <div class="field"><label for="x_cycle">Se cobra</label><select id="x_cycle" name="cycle">${cat.cycles.map((c) => `<option value="${c.key}" ${c.key === v.cycle ? "selected" : ""}>${c.label}</option>`).join("")}</select></div>
    </div>
    <div class="fields-3">
      <div class="field"><label for="x_billing">Fecha de cobro</label><input id="x_billing" name="billing_date" type="date" required value="${esc(v.billing_date)}">
        <div class="help">La próxima (o la última). Las demás se calculan solas.</div></div>
      <div class="field"><label for="x_start">Suscrita desde <span class="muted">(opcional)</span></label><input id="x_start" name="start_date" type="date" value="${esc(v.start_date || "")}">
        <div class="help">Vacío = ya estaba activa antes.</div></div>
      <div class="field"><label for="x_end">Cancelada desde <span class="muted">(opcional)</span></label><input id="x_end" name="end_date" type="date" value="${esc(v.end_date || "")}">
        <div class="help">Deja de contar desde esa fecha.</div></div>
    </div>
    ${business ? `<div class="fields-3">
      <div class="field"><label for="x_seats">Usuarios / licencias</label><input id="x_seats" name="seats" type="number" min="1" step="1" value="${esc(v.seats)}"></div>
      <div class="field"><label for="x_owner">Responsable</label><input id="x_owner" name="owner" maxlength="120" value="${esc(v.owner)}" placeholder="nombre@empresa.com"></div>
      <div class="field"><label for="x_team">Equipo</label><input id="x_team" name="team" maxlength="80" value="${esc(v.team)}"></div>
    </div>` : ""}
    ${business ? "" : `<fieldset class="field members"><legend>¿De quién es?</legend>
      <div class="member-options" id="memberOptions">${memberOptions(v.member_ids || [])}</div>
      <div class="new-member"><input id="x_newMember" maxlength="60" placeholder="Nueva persona (ej. Jimmy)" aria-label="Nueva persona"><button type="button" class="btn small" id="x_addMember">+ Agregar</button></div>
      <div class="help">Si marcas varias personas, el costo se divide en partes iguales entre ellas.</div></fieldset>`}
    <div class="field"><label for="x_notes">Notas <span class="muted">(opcional)</span></label><textarea id="x_notes" name="notes" maxlength="500" rows="2">${esc(v.notes)}</textarea></div>
    <label class="check"><input type="checkbox" name="active" ${v.active ? "checked" : ""}> Activa</label>
    <p class="result bad" id="expenseError" hidden></p>`;
  const service = $("x_service");
  service.onchange = () => {
    const s = cat.services.find((x) => x.key === service.value);
    const name = $("x_name");
    if (!name.value || cat.services.some((x) => x.name === name.value)) name.value = s.key === "other" ? "" : s.name;
    $("x_category").value = s.category;
    $("x_cycle").value = s.cycle;
    $("x_plans").innerHTML = (s.plans || []).map((p) => `<option value="${esc(p)}">`).join("");
  };
  const addMember = $("x_addMember");
  if (addMember) {
    addMember.onclick = async () => {
      const input = $("x_newMember"); const name = input.value.trim();
      if (!name) { input.focus(); return; }
      try {
        const created = await api("/v1/personal/profiles", { method: "PUT", body: { name } });
        RECUR.profiles = [...RECUR.profiles, created].sort((a, b) => a.name.localeCompare(b.name));
        const checked = [...document.querySelectorAll('#memberOptions input:checked')].map((c) => c.value);
        $("memberOptions").innerHTML = memberOptions([...checked, created.profile_id]);
        input.value = "";
      } catch (err) { toast(err.message, true); }
    };
  }
  $("expenseDialog").showModal();
  (expense ? $("x_amount") : (v.name ? $("x_amount") : $("x_name"))).focus();
}

async function saveExpense(event) {
  event.preventDefault();
  const form = $("expenseForm");
  const err = $("expenseError");
  err.hidden = true;
  if (!form.reportValidity()) return;
  const data = Object.fromEntries(new FormData(form));
  const body = {
    expense_id: RECUR.editing?.expense_id ?? null, name: data.name.trim(), service: data.service, category: data.category,
    plan: data.plan.trim(), amount: data.amount, currency: data.currency, cycle: data.cycle, billing_date: data.billing_date,
    start_date: data.start_date || null, end_date: data.end_date || null, notes: data.notes.trim(), active: data.active === "on",
    ...(RECUR.scope === "business"
      ? { seats: Number(data.seats || 1), owner: data.owner.trim(), team: data.team.trim() }
      : { member_ids: [...document.querySelectorAll('#memberOptions input:checked')].map((c) => c.value) }),
  };
  const btn = $("expenseSave"); btn.disabled = true;
  try {
    await api(`/v1/recurring/${RECUR.scope}`, { method: "PUT", body });
    if (RECUR.scope === "personal") setPrefCurrency(body.currency);
    $("expenseDialog").close();
    toast(RECUR.editing ? "Cambios guardados" : `${body.name} agregada`);
    render();
  } catch (e) {
    err.textContent = e.message; err.hidden = false;
  }
  btn.disabled = false;
}

async function deleteExpense() {
  const e = RECUR.editing;
  if (!e || !confirm(`¿Eliminar "${e.name}"? Si solo la cancelaste, es mejor marcarla como inactiva para conservar el historial.`)) return;
  try {
    await api(`/v1/recurring/${RECUR.scope}/${encodeURIComponent(e.expense_id)}`, { method: "DELETE" });
    $("expenseDialog").close();
    toast(`${e.name} eliminada`);
    render();
  } catch (err) { toast(err.message, true); }
}

// Redibuja las gráficas cuando cambia la densidad de píxeles (zoom del navegador, mover la
// ventana a otro monitor). Si el contenedor no cambia de tamaño, Chart.js no se entera y el
// canvas queda escalado y borroso.
const chartPixelRatio = () => Math.max(2, Math.ceil(window.devicePixelRatio || 1));

function watchPixelRatio() {
  const query = matchMedia(`(resolution: ${window.devicePixelRatio}dppx)`);
  query.addEventListener("change", () => {
    Object.values(state.charts).forEach((chart) => {
      if (!chart) return;
      chart.options.devicePixelRatio = chartPixelRatio();
      chart.resize();
    });
    watchPixelRatio();
  }, { once: true });
}

// Esc: primero cierra un formulario abierto; si no hay ninguno, sale de Configuración.
function onEscape(event) {
  const r = route();
  if (event.key !== "Escape" || r.view !== "configuracion" || $("expenseDialog").open) return;
  const form = $("connForm");
  if (form && form.innerHTML.trim()) { form.innerHTML = ""; return; }
  if (event.target.closest?.("input, textarea, select")) { event.target.blur(); return; }
  location.hash = r.space === "personal" ? "#/personal" : `#/empresa/${state.lastView}`;
}

// ------------------------------------------------------------------ arranque
async function main() {
  state.config = await fetch("/config.json").then((r) => r.json()).catch(() => ({ apiBaseUrl: "", authMode: "local", tenant: "local" }));
  try { state.me = await api("/v1/me"); }
  catch (err) { $("banner").hidden = false; $("banner").textContent = `No se pudo conectar con la API: ${err.message}`; return; }
  if (!location.hash) history.replaceState(null, "", "#/empresa/real");
  setupExpenseDialog();
  document.querySelectorAll("[data-days]").forEach((btn) => btn.addEventListener("click", () => {
    state.days = Number(btn.dataset.days); syncRangeButtons(); render();
  }));
  $("groupBy").addEventListener("change", (e) => { state.groupBy = e.target.value; render(); });
  document.querySelectorAll("[data-table-for]").forEach((btn) => btn.addEventListener("click", () => {
    const el = $(`${btn.dataset.tableFor}-table`); el.hidden = !el.hidden;
    btn.textContent = el.hidden ? "Ver tabla" : "Ocultar tabla";
  }));
  window.addEventListener("hashchange", render);
  document.addEventListener("keydown", onEscape);
  watchPixelRatio();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  await render();
}

window.addEventListener("load", main);
