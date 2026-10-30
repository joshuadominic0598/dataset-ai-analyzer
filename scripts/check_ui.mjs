// Static interface contracts and renderer checks. No browser or network is used.
import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const html = fs.readFileSync(
  new URL("../src/reviso/static/index.html", import.meta.url),
  "utf8",
);
const source = fs.readFileSync(
  new URL("../src/reviso/static/app.js", import.meta.url),
  "utf8",
);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
assert.equal(new Set(ids).size, ids.length, "HTML element IDs must be unique");
const dynamic = new Set(["build-index"]);
for (const match of source.matchAll(/\$\(["']([^"']+)["']\)/g))
  assert(
    ids.includes(match[1]) || dynamic.has(match[1]),
    "Missing element " + match[1],
  );
assert(
  !/<(?:script|link)[^>]*(?:src|href)="https?:/i.test(html),
  "App assets must remain local",
);
assert(
  !/\son(?:click|submit|load|change)=/i.test(html),
  "Inline event handlers conflict with CSP",
);

const element = (id = "") => ({
  id,
  innerHTML: "",
  textContent: "",
  value: "",
  checked: false,
  hidden: false,
  disabled: false,
  selectedOptions: [],
  selectionStart: 0,
  selectionEnd: 0,
  children: [],
  classList: { toggle() {}, add() {}, remove() {} },
  listeners: new Map(),
  addEventListener(type, callback) {
    this.listeners.set(type, callback);
  },
  focus() {},
  showModal() {
    this.open = true;
  },
  close() {
    this.open = false;
  },
  setAttribute() {},
  append(child) {
    this.children.push(child);
  },
  scrollIntoView() {},
  querySelector() {
    return { open: false };
  },
  setRangeText() {},
});
const elements = new Map(ids.map((id) => [id, element(id)]));
elements.get("provider").value = "offline";
elements.get("pair-filter").value = "all";
const preferences = new Map();
const localStorage = {
  getItem(key) {
    return preferences.get(key) ?? null;
  },
  setItem(key, value) {
    preferences.set(key, String(value));
  },
};
const requests = [];
let responseFor = () => ({ accepted: true });
let responseStatus = 200;
const context = vm.createContext({
  document: {
    getElementById: (id) => elements.get(id),
    querySelectorAll: () => [],
    addEventListener() {},
    createElement: () => element(),
  },
  localStorage,
  setTimeout,
  clearTimeout,
  console,
  FileReader: class {
    readAsDataURL(file) {
      this.result =
        "data:text/csv;base64," +
        Buffer.from(file.content || "record_id\n1\n").toString("base64");
      queueMicrotask(() => this.onload());
    }
  },
  fetch: async (path, options = {}) => {
    const headers = new Headers(options.headers);
    requests.push({ path, ...options, headers });
    const payload = await responseFor(path, options);
    return {
      ok: responseStatus >= 200 && responseStatus < 300,
      json: async () => payload,
    };
  },
});
const testSource = source.replace(
  /initialize\(\)\.catch\(\(error\)\s*=>\s*toast\(error\.message,\s*true\)\);\s*$/,
  "globalThis.hooks={state,api,startJob,renderReport,renderSetup,renderExplore,renderFindings,appendMessage,metricTable,resultTable,esc,renderConnections,updateConnection,setView};",
);
vm.runInContext(testSource, context);
const hooks = context.hooks;
hooks.state.token = "contract-test";
await hooks.api("/api/configure", { dataset: "sample" });
assert.equal(requests[0].method, "POST");
assert.equal(requests[0].headers.get("X-Reviso-Token"), "contract-test");
assert.equal(requests[0].headers.get("Content-Type"), "application/json");
assert.deepEqual(JSON.parse(requests[0].body), { dataset: "sample" });
hooks.state.connections = [
  {
    id: "ollama",
    name: "Ollama",
    local: true,
    configured: true,
    model: "qwen3:8b",
    tier: "local",
    base_url: "http://127.0.0.1:11434",
    credential_source: "not_required",
  },
  ...["gemini", "openai", "groq"].map((id) => ({
    id,
    name: id,
    local: false,
    configured: false,
    model: "",
    tier: "auto",
    credential_source: "missing",
  })),
];
hooks.setView("connections");
assert.equal(
  (
    elements
      .get("connection-cards")
      .innerHTML.match(/class="connection-card"/g) || []
  ).length,
  4,
);
assert.equal(
  elements.get("workspace-content").hidden,
  false,
  "Connections must work without a dataset",
);
assert(
  elements.get("connection-cards").innerHTML.includes("ollama pull qwen3:8b"),
);
assert(elements.get("connection-cards").innerHTML.includes('type="password"'));
elements.get("provider").value = "ollama";
hooks.updateConnection();
assert.equal(elements.get("model").value, "qwen3:8b");
assert(
  elements
    .get("mode-note")
    .textContent.includes("No cloud-analysis permission"),
);
hooks.state.catalogs.groq = {
  models: [
    { id: "llama-test", label: "Llama" },
    { id: "qwen-test", label: "Qwen" },
  ],
};
elements.get("provider").value = "groq";
elements.get("model").value = "";
elements.get("model-select").value = "";
hooks.updateConnection();
assert(elements.get("model-select").innerHTML.includes("llama-test"));
assert(elements.get("model-select").innerHTML.includes("qwen-test"));
assert(elements.get("model-select").innerHTML.includes("Custom model ID"));
elements.get("provider").value = "offline";
hooks.updateConnection();
assert.equal(elements.get("model-picker").hidden, true);
const evaluation = JSON.parse(
  fs.readFileSync(
    new URL("../outputs/evaluation.json", import.meta.url),
    "utf8",
  ),
);
const report = evaluation.report;
hooks.state.data = {
  dataset: {
    id: report.dataset_id,
    name: report.dataset_name,
    config: report.config,
  },
  versions: report.versions,
  report,
  discovery: {
    identity_candidates: ["Id"],
    semantic_candidates: ["ListingNotes"],
  },
  index: { indexed: 0, expected: 0, ready: false },
};
hooks.renderReport();
hooks.renderSetup();
hooks.renderExplore();
assert.equal(
  (
    elements.get("version-timeline").innerHTML.match(/class="version-tile /g) ||
    []
  ).length,
  5,
);
assert.equal(
  (elements.get("timeline-chart").innerHTML.match(/<circle /g) || []).length,
  5,
);
assert.equal(
  (elements.get("findings").innerHTML.match(/class="finding"/g) || []).length,
  report.findings.length,
);
assert(elements.get("semantic-hint").textContent.includes("ListingNotes"));
assert(elements.get("field-table").innerHTML.includes("SaleAmount"));

const attack = '<img src=x onerror="alert(1)">';
report.findings[0].title = attack;
report.findings[0].detail = attack;
hooks.renderFindings();
assert(!elements.get("findings").innerHTML.includes("<img"));
assert(elements.get("findings").innerHTML.includes("&lt;img"));
assert(
  !hooks
    .resultTable({ columns: [attack], rows: [[attack]], truncated: false })
    .includes("<img"),
);
hooks.appendMessage("assistant", attack, { Q1: { text: attack } });
assert(
  !elements.get("chat-messages").children.at(-1).innerHTML.includes("<img"),
);
assert(hooks.esc("\"'<>&").includes("&quot;"));
assert(/id="chat-open"\s+class="agent-launcher"/.test(html));
assert(html.includes("/brain.svg"));
assert(html.includes("Analytic Brain"));
assert(html.includes('id="effort"'));
assert(
  elements
    .get("exploration-summary")
    .innerHTML.includes("source record examples"),
);
assert(html.includes("What should we learn from this data?"));
assert(!html.includes("Set the boundaries."));
assert(!html.includes("Do renamed fields mean the same thing?"));
assert(!html.includes("What do the fields mean?"));
assert(!html.includes('id="identity"'));
assert(!html.includes('id="field-roles"'));
assert(!/id="analysis-goal"[^>]*required/.test(html));
const single = JSON.parse(
  fs.readFileSync(
    new URL("../outputs/single-evaluation.json", import.meta.url),
    "utf8",
  ),
).report;
hooks.state.data.report = single;
hooks.state.data.versions = single.versions;
elements.get("pair-filter").value = "all";
hooks.renderReport();
hooks.renderExplore();
assert(!elements.get("story-summary").textContent.includes("at least two"));
assert.equal(
  (
    elements.get("version-timeline").innerHTML.match(/class="version-tile /g) ||
    []
  ).length,
  1,
);
assert(elements.get("stats").innerHTML.includes("Fields explored"));
assert(elements.get("field-table").innerHTML.includes("ordinal"));
assert(!elements.get("findings").innerHTML.includes("V1 → V1"));
async function dispatch(id, type, value) {
  const target = elements.get(id);
  if (value !== undefined) target.value = value;
  const listener = target.listeners.get(type);
  assert(listener, `${id} must handle ${type}`);
  await listener({ target, preventDefault() {} });
}

// The chat and investigation bar must select the same connection and model.
await dispatch("chat-provider", "change", "ollama");
assert.equal(elements.get("provider").value, "ollama");
assert.equal(elements.get("chat-model-select").value, "qwen3:8b");
assert.equal(elements.get("chat-model-picker").hidden, false);
await dispatch("chat-model-select", "change", "__custom__");
assert.equal(elements.get("model").hidden, false);
assert.equal(elements.get("chat-custom-model").hidden, false);
await dispatch("chat-model", "input", "local-custom:8b");
assert.equal(elements.get("model").value, "local-custom:8b");
assert.equal(preferences.get("reviso-model-ollama"), "local-custom:8b");
await dispatch("model", "input", "main-custom:8b");
assert.equal(elements.get("chat-model").value, "main-custom:8b");
await dispatch("provider", "change", "groq");
assert.equal(elements.get("chat-provider").value, "groq");
await dispatch("model-select", "change", "llama-test");
assert.equal(elements.get("chat-model-select").value, "llama-test");
await dispatch("chat-provider", "change", "offline");
assert.equal(elements.get("provider").value, "offline");
assert.equal(elements.get("chat-model-picker").hidden, true);
assert.equal(elements.get("chat-custom-model").hidden, true);
assert.equal(elements.get("model").value, "");

// Saving a connection makes its new default available inside an already-open chat.
await dispatch("chat-provider", "change", "ollama");
const savedGemini = {
  ...hooks.state.connections.find((c) => c.id === "gemini"),
  configured: true,
  model: "gemini-new-model",
  credential_source: "saved",
};
responseFor = () => ({
  connections: hooks.state.connections.map((c) =>
    c.id === "gemini" ? savedGemini : c,
  ),
});
const fields = Object.fromEntries(
  Object.entries({
    api_key: "fake-connection-test-key",
    model: "gemini-new-model",
    tier: "free",
  }).map(([name, value]) => [name, { value }]),
);
const form = {
  dataset: { connection: "gemini" },
  elements: { namedItem: (name) => fields[name] },
  querySelector: () => ({}),
};
elements.set("connection-status-gemini", element("connection-status-gemini"));
await elements.get("connection-cards").listeners.get("submit")({
  target: { closest: () => form },
  preventDefault() {},
});
assert.equal(fields.api_key.value, "");
assert(!Array.from(preferences.values()).includes("fake-connection-test-key"));
assert.equal(
  elements.get("chat-provider").value,
  "ollama",
  "Saving another provider must not silently switch chat",
);
await dispatch("chat-provider", "change", "gemini");
assert.equal(elements.get("model").value, "gemini-new-model");
assert.equal(elements.get("chat-model-select").value, "gemini-new-model");
assert(
  !elements.get("chat-provider").innerHTML.includes("gemini · add API key"),
);

// Model refresh uses the selected chat connection and preserves selection during loading.
let finishRefresh;
responseFor = () =>
  new Promise((resolve) => {
    finishRefresh = resolve;
  });
const refreshing = dispatch("chat-refresh-models", "click");
while (!finishRefresh) await Promise.resolve();
assert.equal(elements.get("chat-refresh-models").disabled, true);
assert.equal(elements.get("refresh-models").disabled, true);
finishRefresh({
  provider: "gemini",
  models: [
    { id: "gemini-new-model", label: "Saved Gemini" },
    { id: "gemini-second-model", label: "Another Gemini" },
  ],
  note: "Test catalog",
});
await refreshing;
assert.equal(elements.get("chat-refresh-models").disabled, false);
assert(
  elements.get("chat-model-select").innerHTML.includes("gemini-second-model"),
);
assert.equal(
  JSON.parse(
    requests.findLast((r) => r.path === "/api/connections/models").body,
  ).provider,
  "gemini",
);
await dispatch("chat-model-select", "change", "gemini-second-model");
assert.equal(elements.get("model").value, "gemini-second-model");

// A chat job uses the selected connection, retains cloud consent, and pins its controls until completion.
hooks.state.id = single.dataset_id;
hooks.state.data.dataset.config.cloud_allowed = false;
const beforeDenied = requests.length;
await assert.rejects(
  hooks.startJob("chat", "Inspect this dataset"),
  /permission/,
);
assert.equal(requests.length, beforeDenied);
hooks.state.data.dataset.config.cloud_allowed = true;
hooks.updateConnection();
assert(elements.get("chat-connection-note").textContent.includes("new report"));
hooks.state.data.report.config.cloud_allowed = true;
hooks.updateConnection();
let finishChat;
responseFor = (path) =>
  path === "/api/chat"
    ? new Promise((resolve) => {
        finishChat = resolve;
      })
    : {
        id: "chat-run",
        status: "completed",
        payload: {
          events: [],
          result: { answer: "The evidence is available.", evidence: {} },
        },
      };
const chatting = hooks.startJob("chat", "Inspect this dataset");
while (!finishChat) await Promise.resolve();
for (const id of [
  "provider",
  "model-select",
  "chat-provider",
  "chat-model-select",
  "chat-model",
  "chat-manage-connections",
])
  assert.equal(elements.get(id).disabled, true, id);
finishChat({ run_id: "chat-run" });
await chatting;
const request = JSON.parse(
  requests.findLast((r) => r.path === "/api/chat").body,
);
assert.equal(request.provider, "gemini");
assert.equal(request.model, "gemini-second-model");
assert.equal(elements.get("chat-provider").disabled, false);
responseStatus = 400;
responseFor = () => ({ error: "Connection rejected" });
await assert.rejects(hooks.startJob("chat", "Retry"), /Connection rejected/);
assert.equal(hooks.state.busy, false);
assert.equal(elements.get("chat-provider").disabled, false);
assert.equal(elements.get("chat-refresh-models").disabled, false);
responseStatus = 200;

// A full five-version dataset must still allow a different CSV to be uploaded as a named dataset.
const housing = {
  id: report.dataset_id,
  name: report.dataset_name,
  config: report.config,
};
hooks.state.id = housing.id;
hooks.state.data = {
  dataset: housing,
  versions: report.versions,
  report,
  discovery: { identity_candidates: [], semantic_candidates: [] },
};
await dispatch("add-versions", "click");
assert.equal(elements.get("upload-dialog").open, true);
assert.equal(elements.get("upload-destination").value, "new");
assert.equal(elements.get("upload-single").checked, true);
assert.equal(elements.get("file-input").multiple, false);
assert.equal(elements.get("upload-files").disabled, true);
elements.get("file-input").files = [
  { name: "notes.pdf", size: 20, type: "application/pdf" },
];
await dispatch("file-input", "change");
assert.equal(elements.get("upload-error").hidden, false);
assert(elements.get("upload-error").textContent.includes("CSV"));
assert.equal(hooks.state.pending.length, 0);
const file = (name) => ({
  name,
  size: 12,
  type: "text/csv",
  content: "record_id\n1\n",
});
elements.get("file-input").files = [file("IT_customer_churn.csv")];
await dispatch("file-input", "change");
assert.equal(hooks.state.pending.length, 1);
assert.equal(elements.get("upload-files").disabled, false);
assert.equal(elements.get("upload-error").hidden, true);
assert(
  elements
    .get("pending-files")
    .innerHTML.includes('aria-label="Version 1 label"'),
);
assert.equal(elements.get("upload-dataset-name").value, "IT_customer_churn");
elements.get("upload-dataset-name").value = "Customer churn";

// The same dialog supports multiple versions and explicitly ordered submission.
await dispatch("upload-versions", "change");
assert.equal(elements.get("file-input").multiple, true);
elements.get("file-input").files = [file("February.csv")];
await dispatch("file-input", "change");
assert.equal(hooks.state.pending.length, 2);
const moveSecond = { dataset: { fileUp: "1" } };
await elements.get("pending-files").listeners.get("click")({
  target: { closest: () => moveSecond },
});
assert.equal(hooks.state.pending[0].label, "February");
const submitted = [];
let createCount = 0;
let rejectSecond = true;
const created = {
  id: "churn-dataset",
  name: "Customer churn",
  config: {
    ...single.config,
    setup_complete: false,
    identity: [],
    important: [],
    semantic: [],
    column_roles: {},
    category_orders: {},
    date_column: "",
    target_column: "",
    rename_decisions: [],
  },
};
const savedVersions = [];
responseFor = (path, options) => {
  if (path === "/api/datasets") {
    createCount += 1;
    assert.equal(JSON.parse(options.body).name, "Customer churn");
    return created;
  }
  if (path === "/api/upload") {
    const body = JSON.parse(options.body);
    submitted.push(body);
    assert.equal(body.dataset, created.id);
    if (savedVersions.length === 1 && rejectSecond)
      throw new Error("The second file could not be imported.");
    const version = {
      ...single.versions[0],
      id: `uploaded-${savedVersions.length + 1}`,
      ordinal: savedVersions.length + 1,
      label: body.label,
      columns: ["record_id"],
      profile: { record_id: single.versions[0].profile.record_id },
      row_count: 1,
    };
    savedVersions.push(version);
    return version;
  }
  if (path === "/api/state")
    return {
      token: "contract-test",
      datasets: [created, housing],
      connections: hooks.state.connections,
    };
  if (path.startsWith("/api/dataset?"))
    return {
      dataset: created,
      versions: [...savedVersions],
      discovery: {
        identity_candidates: ["record_id"],
        semantic_candidates: [],
        field_suggestions: {},
      },
      report: null,
      report_stale: false,
      index: null,
    };
  throw new Error("Unexpected upload test request: " + path);
};
elements.get("upload-dataset-name").value = "Customer churn";
await dispatch("upload-files", "click");
assert.equal(createCount, 1);
assert.equal(savedVersions.length, 1);
assert.equal(
  submitted[0].filename,
  "February.csv",
  "Files must be submitted in the arranged order",
);
assert.equal(hooks.state.pending.length, 1);
assert.equal(elements.get("upload-destination").value, "current");
assert.equal(elements.get("upload-error").hidden, false);
assert(elements.get("upload-error").textContent.includes("second file"));
assert.equal(elements.get("upload-files").disabled, false);
rejectSecond = false;
await dispatch("upload-files", "click");
assert.equal(
  createCount,
  1,
  "Retry must reuse the created dataset rather than duplicate it",
);
assert.equal(savedVersions.length, 2);
assert.equal(savedVersions[1].label, "IT_customer_churn");
assert.equal(hooks.state.pending.length, 0);
assert.equal(elements.get("upload-dialog").open, false);
assert.equal(hooks.state.uploading, false);
assert.equal(hooks.state.view, "setup");
assert.equal(hooks.state.id, created.id);
assert.equal(housing.name, report.dataset_name);

// Sidebar New dataset opens the name + file workflow; single mode cannot submit several files.
await dispatch("new-dataset", "click");
assert.equal(elements.get("upload-destination").value, "new");
assert.equal(elements.get("upload-new-dataset").hidden, false);
elements.get("file-input").files = [file("old.csv"), file("new.csv")];
await dispatch("file-input", "change");
assert.equal(elements.get("upload-files").disabled, true);
assert(elements.get("upload-error").textContent.includes("Multiple versions"));
await dispatch("upload-versions", "change");
await dispatch("file-input", "change");
assert.equal(hooks.state.pending.length, 2);
await dispatch("upload-single", "change");
assert.equal(elements.get("upload-files").disabled, true);
assert(elements.get("upload-error").textContent.includes("Several files"));
// The workspace selector switches reports, profiles, SQL context, and conversation history.
elements.get("upload-dialog").close();
responseFor = (path) => {
  if (path === "/api/dataset?dataset=" + housing.id)
    return {
      dataset: housing,
      versions: report.versions,
      report,
      discovery: {
        identity_candidates: ["Id"],
        semantic_candidates: ["ListingNotes"],
      },
      index: { indexed: 0, expected: 0, ready: false },
    };
  if (path === "/api/history?dataset=" + housing.id)
    return {
      messages: [{ role: "user", content: "Housing-specific question" }],
    };
  if (path === "/api/dataset?dataset=" + created.id)
    return {
      dataset: created,
      versions: savedVersions,
      report: null,
      discovery: {
        identity_candidates: ["record_id"],
        semantic_candidates: [],
      },
      index: null,
    };
  throw new Error("Unexpected dataset switch request: " + path);
};
elements.get("query-input").value = "SELECT * FROM old_version";
elements.get("query-result").innerHTML = "Old results";
await dispatch("dataset-select", "change", housing.id);
assert.equal(hooks.state.id, housing.id);
assert.equal(hooks.state.data.dataset.name, housing.name);
assert(!elements.get("query-input").value.includes("old_version"));
assert(
  elements.get("query-input").value.includes(report.versions.at(-1).table),
);
assert.equal(elements.get("query-result").innerHTML, "");
assert.equal(elements.get("version-count").textContent, 5);
assert(elements.get("field-table").innerHTML.includes("GrLivArea"));
assert(elements.get("chat-context").textContent.startsWith(housing.name));
assert(
  elements
    .get("chat-messages")
    .children.at(-1)
    .innerHTML.includes("Housing-specific question"),
);
assert.equal(preferences.get("reviso-dataset"), housing.id);
await dispatch("dataset-select", "change", created.id);
assert.equal(hooks.state.data.dataset.name, "Customer churn");
assert.equal(elements.get("version-count").textContent, 2);
assert(elements.get("field-table").innerHTML.includes("record_id"));
assert(!elements.get("field-table").innerHTML.includes("SaleAmount"));
assert(elements.get("chat-context").textContent.includes("Dataset ready"));
assert.equal(preferences.get("reviso-dataset"), created.id);
await dispatch("add-versions", "click");
assert.equal(elements.get("upload-destination").value, "current");
assert.equal(elements.get("upload-single").disabled, true);
elements.get("file-input").files = Array.from({ length: 4 }, (_, index) =>
  file(`extra-${index}.csv`),
);
await dispatch("file-input", "change");
assert.equal(hooks.state.pending.length, 0);
assert(elements.get("upload-error").textContent.includes("3 more versions"));
assert(html.includes("Model usage"));
assert(!html.includes("Run activity"));

// A blank prompt starts a real report workflow; no technical setup answers are required.
const zeroConfig = {
  ...created.config,
  analysis_goal: "",
  setup_complete: false,
  cloud_allowed: false,
};
const zeroDataset = { ...created, config: zeroConfig };
const zeroReport = {
  ...single,
  dataset_id: created.id,
  dataset_name: created.name,
  config: { ...zeroConfig, setup_complete: true },
};
hooks.state.data = {
  dataset: zeroDataset,
  versions: single.versions,
  report: null,
  discovery: { identity_candidates: [], semantic_candidates: [] },
  index: null,
};
hooks.state.view = "setup";
hooks.renderSetup();
await dispatch("provider", "change", "offline");
let reportRequests = 0;
let savedPreferences;
responseFor = (path, options) => {
  if (path === "/api/configure") {
    savedPreferences = JSON.parse(options.body).config;
    return {
      ...zeroDataset,
      config: { ...zeroDataset.config, ...savedPreferences },
    };
  }
  if (path === "/api/report") {
    reportRequests += 1;
    return { run_id: "no-setup-report" };
  }
  if (path === "/api/run?id=no-setup-report")
    return {
      id: "no-setup-report",
      status: "completed",
      payload: { events: [], result: { report_id: zeroReport.id } },
    };
  if (path === "/api/dataset?dataset=" + created.id)
    return {
      dataset: { ...zeroDataset, config: zeroReport.config },
      versions: single.versions,
      report: zeroReport,
      discovery: { identity_candidates: [], semantic_candidates: [] },
      index: null,
    };
  if (path === "/api/history?dataset=" + created.id) return { messages: [] };
  throw new Error("Unexpected automatic report request: " + path);
};
await dispatch("setup-form", "submit");
assert.equal(reportRequests, 1);
assert.equal(savedPreferences.analysis_goal, "");
assert.equal(savedPreferences.setup_complete, true);
assert(!("identity" in savedPreferences));
assert(!("column_roles" in savedPreferences));
assert(!("rename_decisions" in savedPreferences));
assert(!("cloud_allowed" in savedPreferences));
assert.equal(hooks.state.view, "report");
assert.equal(hooks.state.busy, false);
elements.get("analysis-goal").value = "Which prices stand out?";
await dispatch("generate-report", "click");
assert.equal(reportRequests, 2);
assert.equal(savedPreferences.analysis_goal, "Which prices stand out?");

// Cloud permission stays beside the model controls, synchronized in chat and scoped to the dataset.
await dispatch("provider", "change", "gemini");
assert.equal(elements.get("cloud-permission").hidden, false);
assert.equal(elements.get("chat-cloud-permission").hidden, false);
assert.equal(elements.get("chat-cloud-allowed").checked, false);
elements.get("chat-cloud-allowed").checked = true;
await dispatch("chat-cloud-allowed", "change");
assert.equal(hooks.state.data.dataset.config.cloud_allowed, true);
assert.equal(elements.get("cloud-allowed").checked, true);
assert.equal(hooks.state.data.report_stale, true);
elements.get("cloud-allowed").checked = false;
await dispatch("cloud-allowed", "change");
assert.equal(elements.get("chat-cloud-allowed").checked, false);
await dispatch("provider", "change", "ollama");
assert.equal(elements.get("cloud-permission").hidden, true);
assert.equal(elements.get("chat-cloud-permission").hidden, true);

// Chat acknowledges submission immediately, then keeps real progress visible until the answer arrives.
await dispatch("provider", "change", "offline");
const until = async (condition) => {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail("The chat workflow did not reach the expected state");
};
let acceptQuestion;
const polls = [];
let chatSubmissions = 0;
responseFor = (path) => {
  if (path === "/api/chat") {
    chatSubmissions += 1;
    return new Promise((resolve) => {
      acceptQuestion = resolve;
    });
  }
  if (path === "/api/run?id=visible-chat")
    return new Promise((resolve) => polls.push(resolve));
  throw new Error("Unexpected chat feedback request: " + path);
};
elements.get("chat-input").value = "Which records need attention?";
const sendQuestion = dispatch("chat-form", "submit");
await until(() => !!acceptQuestion);
assert(
  elements
    .get("chat-messages")
    .children.at(-1)
    .innerHTML.includes("Which records need attention?"),
);
assert.equal(elements.get("chat-input").value, "");
assert.equal(elements.get("chat-run-status").hidden, false);
assert.equal(
  elements.get("chat-run-heading").textContent,
  "Sending your question",
);
assert.equal(elements.get("chat-send").disabled, true);
elements.get("chat-input").value = "A draft for my next question";
await dispatch("chat-form", "submit");
assert.equal(
  chatSubmissions,
  1,
  "Repeated submission must not duplicate an active request",
);
assert.equal(elements.get("chat-input").value, "A draft for my next question");
acceptQuestion({ run_id: "visible-chat" });
await until(() => polls.length === 1);
assert.equal(
  elements.get("chat-run-heading").textContent,
  "Agent is responding",
);
assert(elements.get("chat-run-detail").textContent.includes("Question sent"));
const originalDataset = hooks.state.id;
hooks.state.id = "another-dataset";
await dispatch("chat-open", "click");
assert.equal(
  elements.get("chat-run-status").hidden,
  true,
  "Pending replies belong to their dataset",
);
hooks.state.id = originalDataset;
await dispatch("chat-open", "click");
assert.equal(elements.get("chat-run-status").hidden, false);
polls[0]({
  id: "visible-chat",
  status: "running",
  payload: {
    events: [
      { kind: "rate_limit_wait", detail: { seconds: 15, reason: "retry" } },
    ],
  },
});
await until(() => polls.length === 2);
assert(
  elements
    .get("chat-run-detail")
    .textContent.includes("Respecting provider limits"),
);
assert.equal(elements.get("chat-run-status").hidden, false);
polls[1]({
  id: "visible-chat",
  status: "completed",
  payload: {
    events: [],
    result: { answer: "Here are the records to inspect.", evidence: {} },
  },
});
await sendQuestion;
assert(
  elements
    .get("chat-messages")
    .children.at(-1)
    .innerHTML.includes("Here are the records to inspect."),
);
assert.equal(elements.get("chat-run-status").hidden, true);
assert.equal(elements.get("chat-send").disabled, false);
assert.equal(elements.get("chat-send").textContent, "↑");
assert.equal(elements.get("chat-input").value, "A draft for my next question");
assert.equal(hooks.state.chatPending, null);

// A failed answer clears the indicator, shows the error, and leaves the question ready to retry.
responseFor = (path) =>
  path === "/api/chat"
    ? { run_id: "failed-chat" }
    : {
        id: "failed-chat",
        status: "failed",
        payload: {
          events: [],
          error: "The provider could not complete this answer.",
        },
      };
elements.get("chat-input").value = "Please retry this question";
await dispatch("chat-form", "submit");
assert.equal(elements.get("chat-run-status").hidden, true);
assert.equal(elements.get("chat-send").disabled, false);
assert.equal(elements.get("chat-input").value, "Please retry this question");
assert(
  elements.get("chat-messages").children.at(-1).className.includes("error"),
);
assert(
  elements
    .get("chat-messages")
    .children.at(-1)
    .innerHTML.includes("could not complete"),
);

console.log(
  "Static UI contracts and real-report renderer checks passed. No browser visual checks were performed.",
);
