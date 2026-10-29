"use strict";

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const fmt = (n) =>
  n == null
    ? "—"
    : typeof n === "number"
      ? n.toLocaleString(undefined, { maximumFractionDigits: 2 })
      : String(n);
const when = (value) =>
  value
    ? new Date(value).toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      })
    : "—";
const human = (value) => String(value).replaceAll("_", " ");
const state = {
  token: "",
  datasets: [],
  connections: [],
  catalogs: {},
  id: null,
  data: null,
  view: "report",
  severity: "all",
  pending: [],
  busy: false,
  lastChatRun: null,
};
let toastTimer;

function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      $("toast").hidden = true;
    },
    error ? 8500 : 4500,
  );
}

async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Reviso-Token": state.token,
          },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(data.error || "The request could not be completed.");
  return data;
}

function runAction(action) {
  return async (event) => {
    try {
      await action(event);
    } catch (error) {
      toast(error.message, true);
    }
  };
}
const report = () => state.data?.report;
const ds = () => state.data?.dataset;
const currentVersions = () => state.data?.versions || [];
const shownVersions = () => report()?.versions || currentVersions();
const selected = (id) => Array.from($(id).selectedOptions, (o) => o.value);
const evidenceNames = {
  attention: "Needs attention",
  review: "Worth reviewing",
  improved: "Potential improvement",
  info: "Observed change",
};

async function initialize() {
  const data = await api("/api/state");
  Object.assign(state, {
    token: data.token,
    datasets: data.datasets,
    connections: data.connections,
  });
  renderConnections();
  updateConnection();
  const saved = localStorage.getItem("reviso-dataset");
  state.id = data.datasets.some((d) => d.id === state.id)
    ? state.id
    : data.datasets.some((d) => d.id === saved)
      ? saved
      : data.datasets[0]?.id || null;
  $("dataset-select").innerHTML = data.datasets.length
    ? data.datasets
        .map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
        .join("")
    : '<option value="">No datasets yet</option>';
  $("dataset-select").value = state.id || "";
  if (data.watcher)
    $("footer-status").textContent =
      `Watching ${data.watcher.file} · ${data.watcher.status}`;
  if (state.id) await loadDataset();
  else {
    renderEmpty();
    setView(state.view);
  }
}

async function loadDataset() {
  const id = state.id;
  const data = await api("/api/dataset?dataset=" + encodeURIComponent(id));
  if (state.id !== id) return;
  state.data = data;
  localStorage.setItem("reviso-dataset", id);
  $("breadcrumb-name").textContent = data.dataset.name;
  $("page-title").textContent = data.dataset.name;
  $("page-description").textContent = report()
    ? `A closer look at ${report().versions.length} versions · Report generated ${when(report().created_at)}`
    : "Your data is preserved. Tell Analytic Brain what you want to learn, or open the analyst chat.";
  $("version-count").textContent = currentVersions().length;
  $("stale-banner").hidden = !data.report_stale;
  $("export-open").hidden = !report();
  $("workspace-content").hidden = !currentVersions().length;
  $("empty-state").hidden = !!currentVersions().length;
  updateConnection();
  renderReport();
  renderSetup();
  renderExplore();
  await loadHistory();
  if (state.view === "activity") await renderActivity();
  setView(state.view);
}

function renderEmpty() {
  state.data = null;
  $("empty-state").hidden = false;
  $("workspace-content").hidden = true;
  $("version-count").textContent = "0";
  $("export-open").hidden = true;
}

function setView(view) {
  state.view = view;
  document
    .querySelectorAll(".nav-item")
    .forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  document.querySelectorAll(".view").forEach((v) => {
    v.hidden = v.id !== "view-" + view;
  });
  const globalView = view === "connections";
  $("workspace-content").hidden = !globalView && !currentVersions().length;
  $("empty-state").hidden = globalView || !!currentVersions().length;
  $("analysis-bar").hidden = globalView || !currentVersions().length;
  $("add-versions").hidden = globalView;
  $("export-open").hidden = globalView || !report();
  $("stale-banner").hidden = globalView || !state.data?.report_stale;
  if (globalView) {
    $("page-title").textContent = "Choose your models.";
    $("page-description").textContent =
      "Local Ollama or your Gemini, OpenAI, and Groq accounts. Switch models whenever you investigate.";
    $("breadcrumb-name").textContent = "Models & connections";
    renderConnections();
  } else {
    $("page-title").textContent = ds()?.name || "Understand your data.";
    $("page-description").textContent = report()
      ? `A closer look at ${report().versions.length} versions · Report generated ${when(report().created_at)}`
      : "Start with one CSV. Explore its patterns or compare up to five versions.";
    $("breadcrumb-name").textContent = ds()?.name || "Getting started";
  }
  if (view === "activity" && state.id && currentVersions().length)
    renderActivity().catch((e) => toast(e.message, true));
}

function updateConnection() {
  const provider = $("provider").value;
  const connection = state.connections.find((c) => c.id === provider);
  const local = provider === "ollama";
  $("model-picker").hidden = provider === "offline";
  $("refresh-models").hidden = provider === "offline";
  $("mode-note").textContent =
    provider === "offline"
      ? "Measured evidence. No model requests."
      : local
        ? "Local inference on your machine. No cloud-analysis permission needed."
        : connection?.configured
          ? `${connection.name} · ${connection.tier === "auto" ? "account billing applies" : connection.tier + " account selected"} · cloud permission required.`
          : "Add your API key in Models & connections.";
  $("chat-mode-label").textContent =
    provider === "offline"
      ? "Local evidence lookup"
      : local
        ? "Ollama · local model"
        : `${provider} · cloud model`;
  renderModelChoices();
}

function modelOptions(provider, chosen) {
  const connection = state.connections.find((c) => c.id === provider);
  const models = [...(state.catalogs[provider]?.models || [])];
  for (const id of [connection?.model, chosen])
    if (id && !models.some((m) => m.id === id))
      models.unshift({
        id,
        label:
          id +
          (state.catalogs[provider]
            ? " · not returned by this catalog"
            : " · default; availability not checked"),
      });
  return (
    '<option value="">Choose a model</option>' +
    models
      .map(
        (m) =>
          `<option value="${esc(m.id)}" ${m.id === chosen ? "selected" : ""}>${esc(m.label)}${m.installed ? " · installed" : ""}</option>`,
      )
      .join("") +
    '<option value="__custom__">Custom model ID…</option>'
  );
}

function renderModelChoices() {
  const provider = $("provider").value,
    connection = state.connections.find((c) => c.id === provider);
  if (provider === "offline") {
    $("model").value = "";
    $("model").hidden = true;
    return;
  }
  const chosen =
    $("model").value ||
    localStorage.getItem("reviso-model-" + provider) ||
    connection?.model ||
    (provider === "ollama" ? "qwen3:8b" : "");
  const custom = $("model-select").value === "__custom__" && !!$("model").value;
  $("model-select").innerHTML = modelOptions(provider, chosen);
  $("model-select").value = custom ? "__custom__" : chosen;
  $("model").value = chosen;
  $("model").hidden = !custom;
}

function renderConnections() {
  $("connection-cards").innerHTML = state.connections
    .map((c) => {
      const catalog = state.catalogs[c.id];
      const description = c.local
        ? "Run installed text models entirely on this machine. Qwen 8B is the initial default."
        : c.id === "openai"
          ? "Use your OpenAI Platform API key and choose a text model available to that project."
          : c.id === "gemini"
            ? "Connect a Gemini API key from a free or paid project. Choose from its current text models."
            : "Choose from Groq text models available to your free or paid API account.";
      const status = c.local
        ? "No API key required"
        : c.configured
          ? `Key configured · ${c.credential_source === "saved" ? "saved locally" : "server environment"}`
          : "API key needed";
      return `<form class="connection-card" data-connection="${esc(c.id)}"><div class="connection-card-heading"><h3>${esc(c.name)}</h3><span class="tag ${c.local ? "" : "neutral"}">${c.local ? "LOCAL" : "API CONNECTION"}</span></div><p>${esc(description)}</p>${c.local ? `<label>Ollama address<input name="base_url" type="url" value="${esc(c.base_url)}" required></label><div class="field-hint">Start Ollama, then refresh installed models. To install the default: <code>ollama pull qwen3:8b</code>. Model downloads are never started automatically.</div>` : `<label>API key<input name="api_key" type="password" autocomplete="off" spellcheck="false" placeholder="${c.configured ? "Leave blank to keep the configured key" : "Enter your API key"}"></label><label>API account tier<select name="tier"><option value="auto" ${c.tier === "auto" ? "selected" : ""}>Use my provider account</option>${c.id !== "openai" ? `<option value="free" ${c.tier === "free" ? "selected" : ""}>Free tier account</option>` : ""}<option value="paid" ${c.tier === "paid" ? "selected" : ""}>Paid tier account</option></select></label>`}<label>Default model<input name="model" list="model-list-${esc(c.id)}" value="${esc(c.model)}" placeholder="Refresh models or enter a model ID"><datalist id="model-list-${esc(c.id)}">${(catalog?.models || []).map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`).join("")}</datalist></label><div id="connection-status-${esc(c.id)}" class="connection-card-status" role="status">${esc(status)}${catalog ? " · " + catalog.models.length + " models loaded" : ""}</div><div class="connection-card-actions"><button class="button primary" type="submit">Save connection</button><button class="button secondary" type="button" data-refresh-models="${esc(c.id)}">↻ Refresh models</button>${c.credential_source === "saved" ? `<button class="text-button" type="button" data-forget-key="${esc(c.id)}">Remove saved key</button>` : ""}</div><small id="catalog-note-${esc(c.id)}">${esc(catalog?.note || "Refresh loads model metadata only. It does not send dataset content or test generation.")}</small></form>`;
    })
    .join("");
}

async function refreshModels(provider = $("provider").value) {
  const button = $("refresh-models"),
    status = $("connection-status-" + provider);
  button.disabled = true;
  if (status) status.textContent = "Loading models from " + provider + "…";
  try {
    const catalog = await api("/api/connections/models", { provider });
    state.catalogs[provider] = catalog;
    if ($("provider").value === provider) renderModelChoices();
    const list = $("model-list-" + provider);
    if (list)
      list.innerHTML = catalog.models
        .map((m) => `<option value="${esc(m.id)}">${esc(m.label)}</option>`)
        .join("");
    if (status)
      status.textContent = `${catalog.models.length} ${provider === "ollama" ? "installed local" : "provider"} text models loaded${catalog.truncated ? " · partial list" : ""}.`;
    const note = $("catalog-note-" + provider);
    if (note) note.textContent = catalog.note;
    toast(
      catalog.models.length +
        " models loaded. Choose one for your investigation.",
    );
  } catch (error) {
    if (status) status.textContent = error.message;
    throw error;
  } finally {
    button.disabled = false;
  }
}

async function saveConnection(event) {
  const form = event.target.closest("form[data-connection]");
  if (!form) return;
  event.preventDefault();
  const provider = form.dataset.connection;
  const value = (name) => form.elements.namedItem(name)?.value || "";
  const settings = {
    model: value("model"),
    ...(provider === "ollama"
      ? { base_url: value("base_url") }
      : { api_key: value("api_key"), tier: value("tier") }),
  };
  const result = await api("/api/connections/save", { provider, settings });
  state.connections = result.connections;
  delete state.catalogs[provider];
  const key = form.elements.namedItem("api_key");
  if (key) key.value = "";
  const saved = state.connections.find((c) => c.id === provider);
  localStorage.setItem("reviso-model-" + provider, saved.model);
  if ($("provider").value === provider) {
    $("model").value = saved.model;
    $("model-select").value = "";
    updateConnection();
  }
  if (
    saved.credential_source === "saved" &&
    !form.querySelector("[data-forget-key]")
  )
    form
      .querySelector(".connection-card-actions")
      .insertAdjacentHTML(
        "beforeend",
        `<button class="text-button" type="button" data-forget-key="${esc(provider)}">Remove saved key</button>`,
      );
  $("connection-status-" + provider).textContent =
    "Connection saved. Refresh models to check availability.";
  toast("Connection saved locally.");
}

function stat(label, value, foot, icon, extra = "") {
  return `<article class="stat ${extra}"><div class="stat-label">${esc(label)}<span class="stat-icon">${icon}</span></div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${foot}</div></article>`;
}

function renderReport() {
  const r = report(),
    versions = shownVersions(),
    last = versions.at(-1);
  const summary = r?.summary;
  $("stats").innerHTML =
    stat(
      "Files in analysis",
      versions.length.toString().padStart(2, "0"),
      "Up to <b>5</b> snapshots per timeline",
      "▱",
    ) +
    stat(
      "Latest records",
      fmt(last?.row_count || 0),
      `${versions.length > 1 ? esc(fmt((last?.row_count || 0) - versions[0].row_count)) + " net change since V1" : "Your first snapshot is preserved"}`,
      "▤",
    ) +
    stat(
      "Needs attention",
      fmt(summary?.attention || 0),
      "Screening signals to verify at the source",
      "↗",
      "attention",
    ) +
    (versions.length === 1
      ? stat(
          "Fields explored",
          fmt(last?.columns.length || 0),
          "Suggested meanings and full-file profiles",
          "▦",
        )
      : stat(
          "Potential improvements",
          fmt(summary?.improvements || 0),
          "Fields with fewer empty cells",
          "↖",
        ));
  $("report-mode").textContent = r?.ai
    ? "AI INTERPRETATION"
    : "MEASURED EVIDENCE";
  $("story-title").textContent = !r
    ? "Your analyst is ready to explore."
    : versions.length === 1
      ? "A closer look at your data."
      : summary.attention > 0
        ? "Some findings deserve a closer look."
        : "Here’s what the data reveals.";
  $("story-summary").textContent = !r
    ? "One file is enough. Complete dataset setup and generate a report, or ask your analyst to help decide what to investigate. More versions add comparisons."
    : r.ai
      ? r.ai.summary
      : `${summary.versions} file(s) reveal ${r.findings.length} observations and screening signals. ${summary.attention} need attention, and ${summary.improvements} suggest better completeness. A change can be expected or problematic; inspect its evidence before deciding.`;
  $("story-insights").innerHTML = r?.ai
    ? r.ai.insights
        .map(
          (i) =>
            `<div class="insight">${esc(i.interpretation)} ${i.evidence_ids.map((id) => `<button class="citation" data-evidence="${esc(id)}">${esc(id)}</button>`).join("")}<div class="muted">Next: ${esc(i.next_step)}</div></div>`,
        )
        .join("")
    : "";
  $("story-provenance").textContent = r
    ? `Pinned to ${r.versions.length} source fingerprints.`
    : "Start with a measured exploration.";
  $("timeline-limit").textContent = `${versions.length} / 5 versions`;
  $("timeline-chart").innerHTML = chart(versions);
  $("version-timeline").innerHTML = versions
    .map((v, i) => {
      const delta = i ? v.row_count - versions[i - 1].row_count : null;
      return `<button class="version-tile ${i === versions.length - 1 ? "latest" : ""}" data-version="${v.ordinal}" title="${esc(v.label)}"><div class="version-meta"><span class="version-number">V${v.ordinal}</span><span>${i === versions.length - 1 ? "LATEST" : "SNAPSHOT"}</span></div><h3>${esc(v.label.replace(/^\d+\s*·\s*/, ""))}</h3><p><b>${fmt(v.row_count)}</b> records · ${v.columns.length} fields</p><div class="version-delta">${delta === null ? "Starting point" : (delta >= 0 ? "+" : "") + fmt(delta) + " records from V" + (v.ordinal - 1)}</div></button>`;
    })
    .join("");
  $("pair-filter").innerHTML =
    '<option value="all">All evidence</option><option value="snapshot">Latest file analysis</option>' +
    (r?.comparisons || [])
      .map((p) => `<option value="${p.to}">V${p.from} → V${p.to}</option>`)
      .join("");
  $("finding-total").textContent = r ? r.findings.length : "";
  $("report-limits").innerHTML = r
    ? `<details><summary>How to read these findings and their limits</summary><ul>${r.limitations.map((v) => "<li>" + esc(v) + "</li>").join("")}</ul><p>Screening method ${esc(r.method_version)} · Report ${esc(r.id)}</p></details>`
    : "";
  renderExploration();
  renderFindings();
}

function renderExploration() {
  const e = report()?.exploration;
  if (!e) {
    $("exploration-summary").innerHTML = "";
    return;
  }
  const fields = Object.entries(e.fields || {});
  const numeric = fields
    .filter(([, p]) => ["continuous", "discrete"].includes(p.role))
    .slice(0, 10);
  const samples = e.record_samples || [];
  const time = e.time_summary;
  $("exploration-summary").innerHTML =
    `<div class="section-heading"><div><div class="eyebrow">EXPLORE THE LATEST FILE</div><h2>Patterns, fields, and records</h2></div><span class="muted">V${e.version} · ${fmt(e.records)} records profiled</span></div><div class="role-chips">${Object.entries(
      e.role_counts || {},
    )
      .map(
        ([role, n]) =>
          `<span class="tag neutral">${esc(human(role))} · ${fmt(n)}</span>`,
      )
      .join(
        "",
      )}</div><p class="muted">Field meanings are suggestions until confirmed in setup. Measurements cover the full file; displayed examples and relationship screening are bounded.</p>${numeric.length ? `<div class="table-card"><div class="table-scroll"><table><thead><tr><th>Numeric field</th><th>Range</th><th>Median</th><th>Empty</th><th>Unusual values</th></tr></thead><tbody>${numeric.map(([c, p]) => `<tr><td>${esc(c)}</td><td>${fmt(p.min)} → ${fmt(p.max)}</td><td>${fmt(p.median)}</td><td>${fmt(p.missing)}</td><td>${p.unusual_values == null ? "Not screened" : fmt(p.unusual_values)}</td></tr>`).join("")}</tbody></table></div><small>First ten numeric measures. Full profiles are in Explore data and the exported evidence.</small></div>` : ""}${time ? `<details class="eda-detail"><summary>Time coverage · ${esc(time.field)} · ${time.available_periods} periods</summary><p>${fmt(time.valid_dates)} valid dates · ${fmt(time.invalid_dates)} invalid · ${fmt(time.missing_dates)} empty. ${esc(time.scope)}</p>${metricTable({ periods: time.periods })}</details>` : ""}<details class="eda-detail"><summary>Inspect source record examples · ${samples.length} shown</summary><p class="muted">Evenly spaced rows, up to 12 fields and 200 characters per cell. Use chat or SQL to investigate a particular record group.</p><pre class="evidence-block">${esc(JSON.stringify(samples, null, 2))}</pre></details>${e.relationships?.length ? `<details class="eda-detail"><summary>Numeric relationships · ${e.relationships.length} observed</summary><p class="muted">Up to 12 numeric fields screened. Association does not establish causation.</p>${metricTable({ relationships: e.relationships })}</details>` : ""}`;
}

function chart(versions) {
  if (!versions.length)
    return '<p class="muted">Add snapshots to see the timeline.</p>';
  const width = 450,
    height = 166,
    left = 39,
    right = 20,
    top = 25,
    bottom = 29;
  const max = Math.max(...versions.map((v) => v.row_count), 1) * 1.12;
  const min = Math.max(0, Math.min(...versions.map((v) => v.row_count)) * 0.65);
  const x = (i) =>
    left +
    (width - left - right) *
      (versions.length === 1 ? 0.5 : i / (versions.length - 1));
  const y = (n) =>
    height - bottom - ((n - min) / (max - min)) * (height - top - bottom);
  const points = versions.map((v, i) => `${x(i)},${y(v.row_count)}`);
  const area = `M ${x(0)} ${height - bottom} L ${points.join(" L ")} L ${x(versions.length - 1)} ${height - bottom} Z`;
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Record counts by version: ${esc(versions.map((v) => "V" + v.ordinal + " " + v.row_count).join(", "))}"><defs><linearGradient id="area-fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="var(--accent)" stop-opacity=".3"/><stop offset="100%" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>${[
    0, 0.5, 1,
  ]
    .map((n) => {
      const value = min + (max - min) * n;
      return `<line class="chart-grid" x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}"/><text class="chart-axis" x="${left - 8}" y="${y(value) + 3}" text-anchor="end">${Math.round(value)}</text>`;
    })
    .join(
      "",
    )}<path d="${area}" fill="url(#area-fill)"/><polyline class="chart-line" points="${points.join(" ")}" fill="none" stroke-width="2" stroke-linejoin="round"/>${versions.map((v, i) => `<circle class="chart-point" cx="${x(i)}" cy="${y(v.row_count)}" r="4" stroke-width="2"/><text class="chart-point-label" x="${x(i)}" y="${y(v.row_count) - 11}" text-anchor="middle">${v.row_count.toLocaleString()}</text><text class="chart-axis" x="${x(i)}" y="${height - 7}" text-anchor="middle">V${v.ordinal}</text>`).join("")}</svg>`;
}

function metricTable(metrics) {
  const rows = Object.entries(metrics || {}).filter(
    ([key]) =>
      !["examples", "added_examples", "removed_examples", "scope"].includes(
        key,
      ),
  );
  return `<table class="evidence-table"><tbody>${rows.map(([key, value]) => `<tr><td>${esc(human(key))}</td><td>${esc(value === null ? "Not available" : Array.isArray(value) ? value.map((v) => (typeof v === "object" ? JSON.stringify(v) : v)).join(", ") || "None" : typeof value === "object" ? JSON.stringify(value) : typeof value === "boolean" ? (value ? "Yes" : "No") : fmt(value))}</td></tr>`).join("")}</tbody></table>`;
}

function renderFindings() {
  const r = report(),
    search = $("finding-search").value.toLowerCase(),
    pair = $("pair-filter").value;
  if (!r) {
    $("findings").innerHTML =
      '<div class="blank-card">Your first report starts here. Complete dataset setup, then select Generate report.</div>';
    return;
  }
  const filtered = r.findings.filter(
    (f) =>
      (state.severity === "all" || f.severity === state.severity) &&
      (pair === "all" ||
        (pair === "snapshot"
          ? f.scope === "snapshot"
          : f.scope !== "snapshot" && String(f.to) === pair)) &&
      (!search ||
        [f.title, f.column, f.kind, f.detail]
          .join(" ")
          .toLowerCase()
          .includes(search)),
  );
  $("findings").innerHTML = filtered.length
    ? filtered
        .map(
          (f) =>
            `<article class="finding" id="finding-${esc(f.id)}"><div class="finding-top"><span class="severity ${esc(f.severity)}">${esc(evidenceNames[f.severity])}</span><span>${esc(f.id)} · ${f.scope === "snapshot" ? "V" + f.to + " · dataset analysis" : "V" + f.from + " → V" + f.to}</span></div><h3>${esc(f.title)}</h3><p>${esc(f.detail)}</p><div class="next-action">↗ ${esc(f.action)}</div><details><summary>Inspect evidence and examples</summary>${metricTable(f.metrics)}${f.examples?.length ? '<h4 class="muted">Example records</h4><pre class="evidence-block">' + esc(JSON.stringify(f.examples, null, 2)) + "</pre>" : ""}</details><div class="finding-bottom"><span>${esc(human(f.assessment))}</span>${f.important ? '<span class="important-badge">· Priority field</span>' : ""}<button class="citation" data-ask-finding="${esc(f.id)}">Ask about this ↗</button></div></article>`,
        )
        .join("")
    : '<div class="blank-card">No findings match these filters.</div>';
}

function revealEvidence(id) {
  if (id.startsWith("Q")) {
    setView("activity");
    toast("The AI query results are recorded in the report run.");
    return;
  }
  state.severity = "all";
  $("pair-filter").value = "all";
  $("finding-search").value = "";
  updateSeverity();
  setView("report");
  renderFindings();
  const target = $("finding-" + id);
  if (target) {
    target.querySelector("details").open = true;
    target.scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

function updateSeverity() {
  document
    .querySelectorAll("[data-severity]")
    .forEach((b) =>
      b.classList.toggle("selected", b.dataset.severity === state.severity),
    );
}

function options(columns, chosen) {
  return columns
    .map(
      (c) =>
        `<option value="${esc(c)}" ${chosen.includes(c) ? "selected" : ""}>${esc(c)}</option>`,
    )
    .join("");
}

function renderSetup() {
  const config = ds()?.config;
  if (!config) return;
  const columns = [...new Set(currentVersions().flatMap((v) => v.columns))];
  const discovery = state.data.discovery;
  $("dataset-type").value = config.dataset_type || "unknown";
  $("row-meaning").value = config.row_meaning || "";
  $("analysis-goal").value = config.analysis_goal || "";
  for (const id of ["target-column", "date-column"])
    $(id).innerHTML =
      '<option value="">Not selected</option>' +
      options(currentVersions().at(-1)?.columns || [], [
        config[id.replace("-", "_")] || "",
      ]);
  $("date-format").value = config.date_format || "auto";
  $("time-grain").value = config.time_grain || "month";
  renderDomainQuestions();
  const roles = [
    "identifier",
    "nominal",
    "ordinal",
    "continuous",
    "discrete",
    "datetime",
    "boolean",
    "text",
    "empty",
  ];
  $("field-roles").innerHTML = columns
    .map((c) => {
      const suggestion = discovery.field_suggestions?.[c];
      return `<tr><td><b>${esc(c)}</b><small>${esc(suggestion?.role || "unclassified")} · ${esc(suggestion?.basis || "Review the field meaning.")}</small></td><td><select data-role-column="${esc(c)}" aria-label="Meaning of ${esc(c)}"><option value="">Use suggestion</option>${roles.map((r) => `<option value="${r}" ${config.column_roles?.[c] === r ? "selected" : ""}>${esc(human(r))}</option>`).join("")}</select></td><td><input data-order-column="${esc(c)}" aria-label="Ordered labels for ${esc(c)}" value="${esc((config.category_orders?.[c] || []).join(" | "))}" placeholder="Low | Medium | High"></td></tr>`;
    })
    .join("");
  $("identity").innerHTML = options(columns, config.identity);
  $("important").innerHTML = options(columns, config.important);
  $("semantic").innerHTML = options(
    columns.filter((c) =>
      currentVersions().some((v) => v.profile[c]?.kind === "text"),
    ),
    config.semantic,
  );
  $("identity-hint").textContent = discovery.identity_candidates.length
    ? "Unique, complete candidates: " +
      discovery.identity_candidates.join(", ") +
      ". Choose based on meaning, not uniqueness alone."
    : "No single field is complete and unique in every version. Consider a combination, or review repeated identities.";
  $("semantic-hint").textContent = discovery.semantic_candidates.length
    ? "Analytic Brain found description-like text in " +
      discovery.semantic_candidates.join(", ") +
      ". May it embed one or more of these for meaning-based search? This is optional."
    : "No description-like text was detected automatically. You can still select a text field if semantic retrieval would be useful.";
  $("domain-context").value = config.context;
  $("cloud-allowed").checked = config.cloud_allowed;
  $("growth-threshold").value = config.growth_threshold;
  $("missing-threshold").value = config.missing_threshold;
  $("embedding-provider").value = config.embedding_provider;
  $("embedding-model").value = config.embedding_model;
  $("setup-status").textContent = config.setup_complete
    ? "Setup saved. You can update these answers at any time."
    : "Answer these questions before creating your first report.";
  $("connection-status").innerHTML = state.connections
    .map(
      (c) =>
        `<div class="connection-status-item"><b>${esc(c.id)}</b><span>${c.configured ? "Key available on server" : "Key not configured"}</span></div>`,
    )
    .join("");
  const links = new Map();
  [
    ...(report()?.comparisons || []),
    ...(report()?.baseline ? [report().baseline] : []),
  ]
    .flatMap((p) => p.possible_renames || [])
    .forEach((link) => links.set(link.old + "→" + link.new, link));
  (config.rename_decisions || []).forEach((link) =>
    links.set(link.old + "→" + link.new, link),
  );
  $("rename-options").innerHTML = links.size
    ? [...links.values()]
        .map((link) => {
          const decision =
            (config.rename_decisions || []).find(
              (d) => d.old === link.old && d.new === link.new,
            )?.decision || "auto";
          return `<label class="rename-option">${esc(link.old)} → ${esc(link.new)}<select data-link-old="${esc(link.old)}" data-link-new="${esc(link.new)}"><option value="auto" ${decision === "auto" ? "selected" : ""}>Use the provisional match</option><option value="confirm" ${decision === "confirm" ? "selected" : ""}>Same meaning — confirmed</option><option value="reject" ${decision === "reject" ? "selected" : ""}>Different meaning — keep separate</option></select></label>`;
        })
        .join("")
    : '<div class="field-hint">Possible renames will appear here after your first report. You can confirm their meaning or keep the fields separate.</div>';
  if (config.semantic.length && report())
    $("connection-status").innerHTML +=
      `<div class="index-action"><button type="button" id="build-index" class="button secondary">Build text index ↗</button><small>${state.data.index?.ready ? "Index ready" : `${state.data.index?.indexed || 0} / ${state.data.index?.expected || 0} text cells indexed for saved report`}</small></div>`;
}

function renderDomainQuestions() {
  const kind = $("dataset-type").value;
  const discovery = state.data?.discovery;
  const questions = discovery?.domain_questions?.[kind] ||
    discovery?.follow_up_questions || [
      "What does one row represent, and what would you like to learn?",
    ];
  const suggestions = (discovery?.domain_suggestions || [])
    .map((s) => human(s.type))
    .join(", ");
  $("domain-questions").innerHTML =
    (suggestions
      ? `<p>Field names suggest ${esc(suggestions)}. Please confirm the subject above.</p>`
      : "") +
    "<b>Your analyst will clarify:</b><ul>" +
    questions.map((q) => "<li>" + esc(q) + "</li>").join("") +
    "</ul><small>Include answers in your goal or context, or discuss them in chat.</small>";
}

async function saveSetup(event) {
  event.preventDefault();
  const config = {
    dataset_type: $("dataset-type").value,
    row_meaning: $("row-meaning").value,
    analysis_goal: $("analysis-goal").value,
    target_column: $("target-column").value,
    date_column: $("date-column").value,
    date_format: $("date-format").value,
    time_grain: $("time-grain").value,
    column_roles: Object.fromEntries(
      Array.from(document.querySelectorAll("[data-role-column]"))
        .filter((s) => s.value)
        .map((s) => [s.dataset.roleColumn, s.value]),
    ),
    category_orders: Object.fromEntries(
      Array.from(document.querySelectorAll("[data-order-column]"))
        .filter((s) => s.value.trim())
        .map((s) => [
          s.dataset.orderColumn,
          s.value.split("|").map((v) => v.trim()),
        ]),
    ),
    identity: selected("identity"),
    important: selected("important"),
    semantic: selected("semantic"),
    context: $("domain-context").value,
    cloud_allowed: $("cloud-allowed").checked,
    growth_threshold: Number($("growth-threshold").value),
    missing_threshold: Number($("missing-threshold").value),
    embedding_provider: $("embedding-provider").value,
    embedding_model: $("embedding-model").value,
    setup_complete: true,
    rename_decisions: Array.from(document.querySelectorAll("[data-link-old]"))
      .filter((s) => s.value !== "auto")
      .map((s) => ({
        old: s.dataset.linkOld,
        new: s.dataset.linkNew,
        decision: s.value,
      })),
  };
  if (
    config.semantic.length &&
    (!config.embedding_provider || !config.embedding_model.trim())
  )
    throw new Error(
      "Choose an embedding provider and model ID, or leave the text columns unselected.",
    );
  if (
    config.semantic.length &&
    config.embedding_provider !== "ollama" &&
    !config.cloud_allowed
  )
    throw new Error(
      "Cloud text embeddings require permission to send the selected text to your provider.",
    );
  await api("/api/configure", { dataset: state.id, config });
  await loadDataset();
  toast("Setup saved. Generate a report to use these answers.");
  setView("report");
}

function renderExplore() {
  const versions = shownVersions(),
    value = $("explore-version").value;
  $("explore-version").innerHTML = versions
    .map(
      (v) =>
        `<option value="${v.ordinal}">V${v.ordinal} · ${esc(v.label)}</option>`,
    )
    .join("");
  $("explore-version").value = versions.some((v) => String(v.ordinal) === value)
    ? value
    : String(versions.at(-1)?.ordinal || "");
  renderFields();
  const source = $("download-snapshot");
  const chosen = selectedVersion();
  if (chosen)
    source.href =
      "/api/source?dataset=" +
      encodeURIComponent(state.id) +
      "&version=" +
      encodeURIComponent(chosen.id);
  if (!$("query-input").value && versions.length)
    $("query-input").value =
      `SELECT *\nFROM "${versions.at(-1).table}"\nLIMIT 20`;
}

function selectedVersion() {
  return (
    shownVersions().find(
      (v) => String(v.ordinal) === $("explore-version").value,
    ) || shownVersions().at(-1)
  );
}

function renderFields() {
  const version = selectedVersion();
  if (!version) return;
  const needle = $("field-search").value.toLowerCase();
  $("download-snapshot").href =
    "/api/source?dataset=" +
    encodeURIComponent(state.id) +
    "&version=" +
    encodeURIComponent(version.id);
  $("field-table-title").textContent =
    `V${version.ordinal} · ${version.columns.length} fields · ${fmt(version.row_count)} records`;
  $("field-table").innerHTML = version.columns
    .filter((c) => c.toLowerCase().includes(needle))
    .map((c) => {
      const p = version.profile[c],
        meaning =
          report()?.exploration?.version === version.ordinal
            ? report().exploration.fields[c]
            : state.data.discovery?.field_suggestions?.[c];
      return `<tr><td>${esc(c)}</td><td><span class="tag neutral">${esc(meaning?.role || "Unclassified")}</span></td><td><span class="tag neutral">${esc(p.kind)}</span></td><td>${fmt(p.missing)} <span class="muted">(${fmt(p.missing_pct)}%)</span></td><td>${fmt(p.unique_values)}</td><td>${meaning?.role && !["continuous", "discrete"].includes(meaning.role) ? esc(meaning.category_order?.join(" → ") || "—") : p.min === null ? "—" : esc(fmt(p.min) + " → " + fmt(p.max)) + "<small>Median " + esc(fmt(meaning?.median)) + "</small>"}</td><td>${p.semantic_candidate ? '<span class="tag">Suggested</span>' : "—"}</td></tr>`;
    })
    .join("");
}

function resultTable(result) {
  return `<div class="query-result-meta">${result.rows.length} displayed records${result.truncated ? " · Results limited to " + result.row_limit + " records" : ""}</div><div class="table-scroll"><table><thead><tr>${result.columns.map((c) => "<th>" + esc(c) + "</th>").join("")}</tr></thead><tbody>${result.rows.map((row) => "<tr>" + row.map((v) => "<td>" + esc(v === null ? "(empty)" : typeof v === "object" ? JSON.stringify(v) : v) + "</td>").join("") + "</tr>").join("")}</tbody></table></div>`;
}

async function runQuery() {
  $("run-query").disabled = true;
  try {
    const result = await api("/api/query", {
      dataset: state.id,
      sql: $("query-input").value,
    });
    $("query-result").innerHTML = resultTable(result);
  } finally {
    $("run-query").disabled = false;
  }
}

async function renderActivity() {
  const id = state.id,
    { runs } = await api("/api/runs?dataset=" + encodeURIComponent(id));
  if (id !== state.id) return;
  const done = runs.filter((r) => r.status === "completed"),
    failed = runs.filter((r) => r.status === "failed");
  const calls = runs.flatMap((r) =>
    r.payload.events.filter((e) => e.kind === "model_call"),
  );
  const usage = calls.map((c) => c.detail.usage).filter(Boolean);
  const tokens = usage.reduce(
    (n, u) =>
      n +
      (u.total_tokens ||
        (u.input_tokens || u.prompt_tokens || 0) +
          (u.output_tokens || u.completion_tokens || 0)),
    0,
  );
  $("activity-stats").innerHTML =
    stat(
      "Completed runs",
      fmt(done.length),
      "Measured workflow completions",
      "✓",
    ) +
    stat(
      "Failed runs",
      fmt(failed.length),
      "Failures stay visible",
      "↗",
      failed.length ? "attention" : "",
    ) +
    stat(
      "Model calls",
      fmt(calls.length),
      "Includes planning and synthesis",
      "✧",
    ) +
    stat(
      "Reported tokens",
      usage.length ? fmt(tokens) : "—",
      "Provider usage only · cost not priced",
      "▤",
    );
  $("run-list").innerHTML = runs.length
    ? runs
        .map(
          (r) =>
            `<details class="run-item"><summary><div class="run-main"><b>${esc(human(r.kind))}</b> <span class="severity ${r.status === "failed" ? "attention" : r.status === "completed" ? "improved" : "review"}">${esc(r.status)}</span><p>${esc(when(r.started_at))} · ${esc(r.payload.provider || "offline")} ${esc(r.payload.model || "")}</p></div><div class="run-metrics"><span>${r.elapsed_ms == null ? "running" : fmt(r.elapsed_ms) + " ms"}</span><span>${r.payload.events.filter((e) => e.kind === "model_call").length} model calls</span><span>⌄</span></div></summary><div class="run-detail">${r.payload.input ? '<p class="run-input">' + esc(r.payload.input) + "</p>" : ""}${r.payload.error ? '<p class="run-error">' + esc(r.payload.error) + "</p>" : ""}<p class="muted">Run ${esc(r.id)} · ${esc(r.payload.prompt_version)} · ${r.payload.provider === "ollama" ? "Local inference · no API token fee" : "Cost: not priced"}</p><p class="muted">Prompt SHA-256: ${esc(r.payload.prompt_hash)}</p>${r.payload.source_hashes ? '<details><summary class="muted">Source fingerprints</summary><pre class="evidence-block">' + esc(r.payload.source_hashes.join("\n")) + "</pre></details>" : ""}${r.payload.events.map((event) => `<div class="run-event"><strong>${esc(human(event.kind))}</strong><time>${esc(when(event.at))}</time><pre class="evidence-block">${esc(JSON.stringify(event.detail, null, 2))}</pre></div>`).join("")}<div class="feedback-row"><button data-feedback="1" data-run="${esc(r.id)}">Useful ↑</button><button data-feedback="-1" data-run="${esc(r.id)}">Needs work ↓</button></div></div></details>`,
        )
        .join("")
    : '<div class="blank-card">Completed investigations and query runs will appear here.</div>';
}

async function startJob(kind, question = "") {
  if (!state.id) throw new Error("Create a dataset first.");
  if (state.busy) throw new Error("An investigation is already running.");
  if (!currentVersions().length)
    throw new Error("Upload at least one CSV to begin.");
  if (kind === "report" && !ds().config.setup_complete) {
    setView("setup");
    toast("Answer the setup questions to guide the first report.");
    return;
  }
  const provider = $("provider").value,
    model = $("model").value.trim();
  if (!["offline", "ollama"].includes(provider) && !ds().config.cloud_allowed) {
    setView("setup");
    throw new Error("Cloud analysis needs your permission in Dataset setup.");
  }
  if (provider !== "offline" && !model)
    throw new Error("Enter a model ID available to your provider account.");
  if (provider !== "offline")
    localStorage.setItem("reviso-model-" + provider, model);
  const id = state.id;
  const investigation = {
    effort: $("effort").value || "auto",
    context_window: Number($("context-window").value) || 16384,
  };
  if (investigation.effort === "high" && investigation.context_window < 32768)
    throw new Error(
      "Select a supported model context of 32k or more for high effort.",
    );
  const { run_id } = await api("/api/" + kind, {
    dataset: id,
    provider,
    model,
    question,
    investigation,
  });
  state.busy = true;
  setBusy(true);
  try {
    const run = await waitForRun(run_id, id);
    if (run.status === "failed")
      throw new Error(run.payload.error || "Investigation failed.");
    if (state.id !== id) return;
    if (kind === "chat") {
      if (!report()) await loadDataset();
      else
        appendMessage(
          "assistant",
          run.payload.result.answer,
          run.payload.result.evidence,
        );
      state.lastChatRun = run.id;
    } else {
      await loadDataset();
      toast(
        kind === "index"
          ? "Text search is ready."
          : "Report saved. Every finding has inspectable evidence.",
      );
    }
    return run;
  } finally {
    state.busy = false;
    setBusy(false);
  }
}

function setBusy(busy) {
  $("progress-panel").hidden = !busy;
  $("generate-report").disabled = busy;
  $("chat-send").disabled = busy;
  $("upload-files").disabled = busy || !state.pending.length;
  $("progress-title").textContent = "Investigating your dataset";
  $("progress-detail").textContent = "Collecting evidence…";
}

async function waitForRun(id, datasetId) {
  let failures = 0;
  while (true) {
    let run;
    try {
      run = await api("/api/run?id=" + encodeURIComponent(id));
      failures = 0;
    } catch (error) {
      if (++failures >= 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, 1500));
      continue;
    }
    if (state.id === datasetId) {
      const latest = run.payload.events.at(-1);
      if (latest)
        $("progress-detail").textContent =
          latest.kind === "model_call"
            ? "Model response received. Checking evidence…"
            : latest.kind === "sql"
              ? "Inspecting a read-only query result…"
              : latest.kind === "embed"
                ? `Embedding selected text · ${latest.detail.cells_this_run} cells completed`
                : human(latest.kind) +
                  " · " +
                  (latest.detail.actions?.[0] ||
                    "Following the saved investigation plan.");
    }
    if (run.status !== "running") return run;
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

function openUpload() {
  state.pending = [];
  renderPending();
  $("file-input").value = "";
  $("upload-steps").innerHTML = currentVersions()
    .map(
      (v) =>
        `<div class="upload-existing"><span>V${v.ordinal}</span>${esc(v.label)} · ${fmt(v.row_count)} records</div>`,
    )
    .join("");
  $("upload-dialog").showModal();
}

function addFiles(files) {
  const available = 5 - currentVersions().length - state.pending.length;
  const accepted = Array.from(files);
  if (accepted.length > available)
    throw new Error(
      `This timeline has room for ${available} more version${available === 1 ? "" : "s"}. Start a new dataset for another timeline.`,
    );
  for (const file of accepted) {
    if (!file.name.toLowerCase().endsWith(".csv"))
      throw new Error("Choose CSV files.");
    if (!file.size) throw new Error(file.name + " is empty.");
    if (file.size > 20 * 1024 * 1024)
      throw new Error(file.name + " exceeds 20 MB.");
  }
  accepted.forEach((file) =>
    state.pending.push({ file, label: file.name.replace(/\.csv$/i, "") }),
  );
  renderPending();
}

function renderPending() {
  $("pending-files").innerHTML = state.pending
    .map(
      (p, i) =>
        `<div class="pending-file"><span>V${currentVersions().length + i + 1}</span><input data-file-label="${i}" aria-label="Version ${currentVersions().length + i + 1} label" maxlength="100" value="${esc(p.label)}"><button data-file-up="${i}" aria-label="Move version up" ${i === 0 ? "disabled" : ""}>↑</button><button data-file-down="${i}" aria-label="Move version down" ${i === state.pending.length - 1 ? "disabled" : ""}>↓</button><button data-file-remove="${i}" aria-label="Remove file">×</button></div>`,
    )
    .join("");
  $("upload-files").disabled = !state.pending.length || state.busy;
}

function base64File(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",")[1]);
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

async function uploadFiles() {
  if (!state.pending.length) return;
  $("upload-files").disabled = true;
  try {
    if (!state.id) {
      const created = await api("/api/datasets", {
        name: state.pending[0].label.slice(0, 100) || "My dataset",
      });
      state.id = created.id;
    }
    while (state.pending.length) {
      const pending = state.pending[0];
      $("upload-files").textContent = "Saving " + pending.label + "…";
      await api("/api/upload", {
        dataset: state.id,
        label: pending.label,
        filename: pending.file.name,
        content_type: pending.file.type,
        content: await base64File(pending.file),
      });
      state.pending.shift();
    }
    $("upload-dialog").close();
    await initialize();
    if (!ds().config.setup_complete) setView("setup");
    toast("Snapshots saved. Set up the investigation, then generate a report.");
  } catch (error) {
    if (state.id) await initialize();
    throw error;
  } finally {
    $("upload-files").textContent = "Save snapshots ↗";
    renderPending();
  }
}

async function loadDemo() {
  $("load-demo").disabled = true;
  try {
    if (!state.id || currentVersions().length) {
      const created = await api("/api/datasets", { name: "Ames housing" });
      state.id = created.id;
    }
    await api("/api/demo", { dataset: state.id });
    await initialize();
    await startJob("report");
  } finally {
    $("load-demo").disabled = false;
  }
}

function openChat(question = "") {
  $("chat-panel").hidden = false;
  $("chat-open").hidden = true;
  $("chat-open").setAttribute("aria-expanded", "true");
  if (question) $("chat-input").value = question;
  $("chat-input").focus();
}

function closeChat() {
  $("chat-panel").hidden = true;
  $("chat-open").hidden = false;
  $("chat-open").setAttribute("aria-expanded", "false");
  $("chat-open").focus();
}

async function loadHistory() {
  $("chat-messages").innerHTML = "";
  const r = report();
  $("chat-context").textContent = r
    ? `${r.dataset_name} · report with ${r.versions.length} versions${state.data.report_stale ? " · earlier saved inputs" : ""}`
    : currentVersions().length
      ? "Dataset ready · ask your analyst to help explore or clarify the analysis."
      : "Upload a CSV to begin.";
  if (!r) {
    appendMessage(
      "assistant",
      "I can help explore a single file or compare versions. What does one row represent, and what would you like to learn? Choose a model above, then ask about patterns, groups, missing values, or specific records.",
    );
    return;
  }
  const id = state.id,
    data = await api("/api/history?dataset=" + encodeURIComponent(id));
  if (id !== state.id) return;
  if (!data.messages.length)
    appendMessage(
      "assistant",
      "Your report is ready. Ask a question to investigate its findings. Evidence-only mode retrieves measured findings; Ollama and cloud models can plan SQL queries and explain the results.",
    );
  else
    data.messages.forEach((m) => appendMessage(m.role, m.content, m.evidence));
}

function appendMessage(role, text, evidence = null, error = false) {
  const container = document.createElement("article");
  container.className = "message " + role + (error ? " error" : "");
  container.innerHTML = `<div class="message-label">${role === "user" ? "You" : "Analytic Brain"}</div><div>${esc(text)}</div>${evidence && Object.keys(evidence).length ? "<details><summary>Inspect cited evidence · " + Object.keys(evidence).map(esc).join(", ") + '</summary><pre class="evidence-block">' + esc(JSON.stringify(evidence, null, 2)) + "</pre></details>" : ""}`;
  $("chat-messages").append(container);
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
}

async function sendChat(event) {
  event.preventDefault();
  const question = $("chat-input").value.trim();
  if (!question) return;
  if (state.busy) return toast("Wait for the current investigation to finish.");
  appendMessage("user", question);
  $("chat-input").value = "";
  try {
    await startJob("chat", question);
  } catch (error) {
    appendMessage("assistant", error.message, null, true);
  }
}

function openExport() {
  const r = report();
  if (!r) return;
  const base =
    "/api/export?dataset=" +
    encodeURIComponent(state.id) +
    "&report=" +
    encodeURIComponent(r.id) +
    "&format=";
  $("export-links").innerHTML =
    `<a href="${base}html" download>HTML report ↗<span>A standalone report you can open, share, or print to PDF</span></a><a href="${base}md" download>Markdown report ↗<span>Portable narrative, findings, and supporting metrics</span></a><a href="${base}json" download>Complete JSON evidence ↗<span>Version fingerprints, comparisons, profiles, and generated interpretation</span></a>`;
  $("export-dialog").showModal();
}

document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => setView(button.dataset.view)),
  );
$("dataset-select").addEventListener(
  "change",
  runAction(async () => {
    state.id = $("dataset-select").value;
    state.view = "report";
    $("query-input").value = "";
    $("query-result").innerHTML = "";
    await loadDataset();
  }),
);
$("provider").value = localStorage.getItem("reviso-provider") || "ollama";
$("effort").value = localStorage.getItem("reviso-effort") || "auto";
$("context-window").value =
  localStorage.getItem("reviso-context") ||
  ($("provider").value === "ollama" ? "16384" : "32768");
$("effort").addEventListener("change", () =>
  localStorage.setItem("reviso-effort", $("effort").value),
);
$("context-window").addEventListener("change", () =>
  localStorage.setItem("reviso-context", $("context-window").value),
);
$("provider").addEventListener("change", () => {
  if (!localStorage.getItem("reviso-context"))
    $("context-window").value =
      $("provider").value === "ollama" ? "16384" : "32768";
  $("model").value = "";
  $("model-select").value = "";
  localStorage.setItem("reviso-provider", $("provider").value);
  updateConnection();
});
$("model-select").addEventListener("change", () => {
  const choice = $("model-select").value;
  const custom = choice === "__custom__";
  $("model").hidden = !custom;
  if (custom) {
    $("model").value = "";
    $("model").focus();
  } else {
    $("model").value = choice;
    localStorage.setItem("reviso-model-" + $("provider").value, choice);
  }
});
$("model").addEventListener("input", () =>
  localStorage.setItem(
    "reviso-model-" + $("provider").value,
    $("model").value.trim(),
  ),
);
$("refresh-models").addEventListener(
  "click",
  runAction(() => refreshModels()),
);
$("manage-connections").addEventListener("click", () => setView("connections"));
$("connection-cards").addEventListener("submit", runAction(saveConnection));
$("connection-cards").addEventListener(
  "click",
  runAction(async (event) => {
    const refresh = event.target.closest("[data-refresh-models]");
    if (refresh) {
      refresh.disabled = true;
      try {
        await refreshModels(refresh.dataset.refreshModels);
      } finally {
        refresh.disabled = false;
      }
      return;
    }
    const forget = event.target.closest("[data-forget-key]");
    if (forget) {
      const provider = forget.dataset.forgetKey;
      const result = await api("/api/connections/save", {
        provider,
        settings: { forget_key: true },
      });
      state.connections = result.connections;
      delete state.catalogs[provider];
      renderConnections();
      updateConnection();
      toast("Saved key removed. Any server environment key remains available.");
    }
  }),
);
$("new-dataset").addEventListener("click", () => {
  $("new-name").value = "";
  $("new-dialog").showModal();
  $("new-name").focus();
});
$("new-close").addEventListener("click", () => $("new-dialog").close());
$("new-form").addEventListener(
  "submit",
  runAction(async (event) => {
    event.preventDefault();
    const created = await api("/api/datasets", { name: $("new-name").value });
    state.id = created.id;
    state.view = "report";
    $("new-dialog").close();
    await initialize();
  }),
);
["add-versions", "empty-upload"].forEach((id) =>
  $(id).addEventListener("click", openUpload),
);
$("load-demo").addEventListener("click", runAction(loadDemo));
$("file-input").addEventListener(
  "change",
  runAction(() => addFiles($("file-input").files)),
);
$("drop-zone").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("drop-zone").classList.add("dragging");
});
$("drop-zone").addEventListener("dragleave", () =>
  $("drop-zone").classList.remove("dragging"),
);
$("drop-zone").addEventListener(
  "drop",
  runAction((event) => {
    event.preventDefault();
    $("drop-zone").classList.remove("dragging");
    addFiles(event.dataTransfer.files);
  }),
);
$("pending-files").addEventListener("input", (event) => {
  if (event.target.dataset.fileLabel !== undefined)
    state.pending[Number(event.target.dataset.fileLabel)].label =
      event.target.value;
});
$("pending-files").addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  let i;
  if (target.dataset.fileRemove !== undefined)
    state.pending.splice(Number(target.dataset.fileRemove), 1);
  if (target.dataset.fileUp !== undefined) {
    i = Number(target.dataset.fileUp);
    [state.pending[i - 1], state.pending[i]] = [
      state.pending[i],
      state.pending[i - 1],
    ];
  }
  if (target.dataset.fileDown !== undefined) {
    i = Number(target.dataset.fileDown);
    [state.pending[i + 1], state.pending[i]] = [
      state.pending[i],
      state.pending[i + 1],
    ];
  }
  renderPending();
});
$("upload-files").addEventListener("click", runAction(uploadFiles));
$("dataset-type").addEventListener("change", renderDomainQuestions);
$("setup-form").addEventListener("submit", runAction(saveSetup));
$("setup-form").addEventListener(
  "click",
  runAction(async (event) => {
    if (event.target.id === "build-index") await startJob("index");
  }),
);
$("generate-report").addEventListener(
  "click",
  runAction(() => startJob("report")),
);
$("severity-filters").addEventListener("click", (event) => {
  const button = event.target.closest("[data-severity]");
  if (button) {
    state.severity = button.dataset.severity;
    updateSeverity();
    renderFindings();
  }
});
$("finding-search").addEventListener("input", renderFindings);
$("pair-filter").addEventListener("change", renderFindings);
$("findings").addEventListener("click", (event) => {
  const b = event.target.closest("[data-ask-finding]");
  if (b)
    openChat(
      "Help me understand finding " +
        b.dataset.askFinding +
        ". What evidence supports it and what should I check?",
    );
});
$("story-insights").addEventListener("click", (event) => {
  const b = event.target.closest("[data-evidence]");
  if (b) revealEvidence(b.dataset.evidence);
});
$("version-timeline").addEventListener("click", (event) => {
  const b = event.target.closest("[data-version]");
  if (b) {
    $("explore-version").value = b.dataset.version;
    renderFields();
    setView("explore");
  }
});
$("field-search").addEventListener("input", renderFields);
$("explore-version").addEventListener("change", renderFields);
$("show-fields").addEventListener("click", () => {
  $("fields-panel").hidden = false;
  $("sql-panel").hidden = true;
  $("show-fields").classList.add("selected");
  $("show-query").classList.remove("selected");
});
$("show-query").addEventListener("click", () => {
  $("fields-panel").hidden = true;
  $("sql-panel").hidden = false;
  $("show-fields").classList.remove("selected");
  $("show-query").classList.add("selected");
});
$("run-query").addEventListener("click", runAction(runQuery));
$("copy-table").addEventListener("click", () => {
  const version = selectedVersion();
  if (version) {
    const input = $("query-input");
    input.setRangeText(
      '"' + version.table + '"',
      input.selectionStart,
      input.selectionEnd,
      "end",
    );
    input.focus();
  }
});
$("refresh-activity").addEventListener("click", runAction(renderActivity));
$("run-list").addEventListener(
  "click",
  runAction(async (event) => {
    const b = event.target.closest("[data-feedback]");
    if (b) {
      await api("/api/feedback", {
        dataset: state.id,
        run_id: b.dataset.run,
        rating: Number(b.dataset.feedback),
      });
      toast("Feedback saved for this run.");
    }
  }),
);
$("export-open").addEventListener("click", openExport);
$("plan-open").addEventListener("click", () => {
  $("plan-list").innerHTML = (
    report()?.plan || [
      "Complete setup and generate a report to inspect the investigation plan.",
    ]
  )
    .map((v) => "<li>" + esc(v) + "</li>")
    .join("");
  $("plan-dialog").showModal();
});
$("chat-open").addEventListener("click", () => openChat());
$("chat-close").addEventListener("click", closeChat);
$("ask-priorities").addEventListener("click", () =>
  openChat("Which changes need attention first, and what checks should I do?"),
);
document
  .querySelectorAll("[data-question]")
  .forEach((b) =>
    b.addEventListener("click", () => openChat(b.dataset.question)),
  );
$("chat-form").addEventListener("submit", sendChat);
$("chat-input").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("chat-panel").hidden) closeChat();
});
initialize().catch((error) => toast(error.message, true));
