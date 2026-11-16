// UsageHub dashboard: SPA sin build step. Lee /config.json (generado por Terraform).
// authMode "cognito": OAuth 2.0 Authorization Code + PKCE contra Cognito Hosted UI.
// authMode "local": servidor de demostración (scripts/local_server.py).

const PROVIDER_ORDER = ["anthropic", "openai", "google", "aws", "custom", "synthetic"];
const PROVIDER_LABEL = { anthropic: "Anthropic (Claude)", openai: "OpenAI", google: "Google (Gemini)", aws: "AWS", custom: "Personalizado", synthetic: "Sintético" };
const state = { days: 30, groupBy: "provider", config: null, charts: {} };

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const seriesColor = (i) => css(`--series-${(i % 8) + 1}`);
const providerColor = (p) => seriesColor(Math.max(PROVIDER_ORDER.indexOf(p), 0));
const usd = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const usd0 = new Intl.NumberFormat("es", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat("es", { notation: "compact", maximumFractionDigits: 1 });
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ auth (PKCE)
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
async function sha256(text) { return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)); }

function storage() { try { return window.sessionStorage; } catch { return null; } }

async function login(cfg) {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const stateParam = b64url(crypto.getRandomValues(new Uint8Array(16)));
  storage()?.setItem("pkce", JSON.stringify({ verifier, state: stateParam }));
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
  history.replaceState({}, "", "/");
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
  // El access token de Cognito incluye cognito:groups; el id token incluye custom:tenant_id.
  return tokens.id_token;
}

async function api(path, params = {}) {
  const cfg = state.config;
  const headers = {};
  const token = await accessToken(cfg);
  if (token) headers.authorization = `Bearer ${token}`;
  if (cfg.authMode === "local") headers["x-tenant-id"] = "demo";
  const res = await fetch(`${cfg.apiBaseUrl}${path}?${new URLSearchParams(params)}`, { headers });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

// ------------------------------------------------------------------ render
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
  $("kpiTeam").textContent = topTeam;
  $("kpiTeamSub").textContent = topCost ? usd.format(topCost) : "";
}

function renderProviders(summary) {
  const total = Number(summary.total.cost) || 1;
  const rows = Object.entries(summary.by_provider).sort((a, b) => PROVIDER_ORDER.indexOf(a[0]) - PROVIDER_ORDER.indexOf(b[0]));
  $("providersTable").tBodies[0].innerHTML = rows.map(([p, m]) => `
    <tr><td><span class="swatch" style="background:${providerColor(p)}"></span>${PROVIDER_LABEL[p] || p}</td>
    <td class="num">${usd.format(Number(m.cost))}</td><td class="num">${compact.format(m.total_tokens)}</td>
    <td class="num">${(100 * Number(m.cost) / total).toFixed(1)}%</td></tr>`).join("");
}

const ICONS = {
  ok: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--status-good)"/><path d="M4.5 8.2l2.2 2.2 4.8-4.8" stroke="#fff" stroke-width="1.8" fill="none"/></svg>',
  warn: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><path d="M8 1l7 13H1z" fill="var(--status-warning)"/><path d="M8 6v4M8 11.5v.5" stroke="#000" stroke-width="1.6"/></svg>',
  over: '<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="7" fill="var(--status-critical)"/><path d="M5 5l6 6M11 5l-6 6" stroke="#fff" stroke-width="1.8"/></svg>',
};

function renderBudgets(budgets) {
  $("budgetsTable").tBodies[0].innerHTML = budgets.items.map((b) => {
    const kind = b.ratio >= 1 ? ["over", "Excedido"] : b.ratio >= 0.8 ? ["warn", "En riesgo"] : ["ok", "En rango"];
    return `<tr><td>${b.team}${b.provider ? ` · ${b.provider}` : ""}</td><td class="num">${usd.format(Number(b.spent))}</td>
      <td class="num">${usd0.format(Number(b.monthly_limit))}</td>
      <td><span class="status">${ICONS[kind[0]]}${kind[1]} (${Math.round(b.ratio * 100)}%)</span></td></tr>`;
  }).join("") || '<tr><td colspan="4">Sin presupuestos configurados</td></tr>';
}

function renderSeries(ts) {
  const ordered = [...ts.series].sort((a, b) => (state.groupBy === "provider" ? PROVIDER_ORDER.indexOf(a.name) - PROVIDER_ORDER.indexOf(b.name) : a.name.localeCompare(b.name)));
  const datasets = ordered.slice(0, 8).map((s, i) => ({
    label: PROVIDER_LABEL[s.name] || s.name,
    data: s.cost.map(Number),
    backgroundColor: state.groupBy === "provider" ? providerColor(s.name) : seriesColor(i),
    borderColor: css("--surface-1"), borderWidth: { top: 2 }, borderRadius: 4, borderSkipped: "bottom", maxBarThickness: 28,
  }));
  $("seriesTitle").textContent = `Costo diario por ${state.groupBy === "provider" ? "proveedor" : "equipo"} (USD)`;
  draw("seriesChart", { type: "bar", data: { labels: ts.days.map((d) => d.slice(5)), datasets },
    options: baseOptions({ scales: { ...baseOptions().scales, x: { ...baseOptions().scales.x, stacked: true }, y: { ...baseOptions().scales.y, stacked: true } } }) });
  $("series-table").innerHTML = `<table class="data"><thead><tr><th>Día</th>${datasets.map((d) => `<th class="num">${d.label}</th>`).join("")}</tr></thead>
    <tbody>${ts.days.map((day, i) => `<tr><td>${day}</td>${datasets.map((d) => `<td class="num">${usd.format(d.data[i])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function renderRanking(id, rows) {
  const grouped = {};
  for (const r of rows) {
    grouped[r.name] ??= {};
    grouped[r.name][r.provider] = (grouped[r.name][r.provider] || 0) + Number(r.cost);
  }
  const names = Object.keys(grouped).sort((a, b) => Object.values(grouped[b]).reduce((x, y) => x + y) - Object.values(grouped[a]).reduce((x, y) => x + y)).slice(0, 8);
  const providers = PROVIDER_ORDER.filter((p) => names.some((n) => grouped[n][p]));
  draw(id, {
    type: "bar",
    data: { labels: names.map((n) => n.replace(/@.*/, "")), datasets: providers.map((p) => ({
      label: PROVIDER_LABEL[p], data: names.map((n) => grouped[n][p] || 0), backgroundColor: providerColor(p),
      borderColor: css("--surface-1"), borderWidth: { right: 2 }, borderRadius: 4, borderSkipped: "left", maxBarThickness: 18 })) },
    options: baseOptions({
      indexAxis: "y", interaction: { mode: "index", intersect: false, axis: "y" },
      plugins: { ...baseOptions().plugins, tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${usd.format(c.parsed.x)}` } } },
      scales: {
        x: { stacked: true, grid: { color: css("--grid") }, ticks: { color: css("--text-muted"), callback: (v) => usd0.format(v) }, border: { display: false } },
        y: { stacked: true, grid: { display: false }, ticks: { color: css("--text-secondary") }, border: { color: css("--border") } },
      },
    }),
  });
}

async function refresh() {
  const r = range();
  try {
    const [summary, ts, budgets] = await Promise.all([
      api("/v1/summary", r), api("/v1/timeseries", { ...r, group_by: state.groupBy }), api("/v1/budgets"),
    ]);
    $("banner").hidden = true;
    renderKpis(summary); renderProviders(summary); renderBudgets(budgets); renderSeries(ts);
    renderRanking("membersChart", summary.top_members);
    renderRanking("servicesChart", summary.top_services);
  } catch (err) {
    $("banner").hidden = false;
    $("banner").textContent = `No se pudieron cargar los datos: ${err.message}`;
  }
}

async function main() {
  state.config = await fetch("/config.json").then((r) => r.json()).catch(() => ({ apiBaseUrl: "", authMode: "local" }));
  $("tenant").textContent = state.config.authMode === "local" ? "· demo local" : "";
  document.querySelectorAll("[data-days]").forEach((btn) => btn.addEventListener("click", () => {
    document.querySelectorAll("[data-days]").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
    state.days = Number(btn.dataset.days); refresh();
  }));
  $("groupBy").addEventListener("change", (e) => { state.groupBy = e.target.value; refresh(); });
  document.querySelectorAll("[data-table-for]").forEach((btn) => btn.addEventListener("click", () => {
    const el = $(`${btn.dataset.tableFor}-table`); el.hidden = !el.hidden;
    btn.textContent = el.hidden ? "Ver tabla" : "Ocultar tabla";
  }));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);
  await refresh();
}

window.addEventListener("load", main);
