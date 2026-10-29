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
  showModal() {},
  close() {},
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
  FileReader: class {},
  fetch: async (path, options = {}) => {
    const headers = new Headers(options.headers);
    requests.push({ path, ...options, headers });
    return {
      ok: responseStatus >= 200 && responseStatus < 300,
      json: async () => responseFor(path, options),
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
assert(elements.get("identity").innerHTML.includes("Id"));
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
assert(elements.get("field-roles").innerHTML.includes("data-role-column"));
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

console.log(
  "Static UI contracts and real-report renderer checks passed. No browser visual checks were performed.",
);
