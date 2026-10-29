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
  addEventListener() {},
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
const localStorage = {
  getItem() {
    return null;
  },
  setItem() {},
};
const requests = [];
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
    return { ok: true, json: async () => ({ accepted: true }) };
  },
});
const testSource = source.replace(
  /initialize\(\)\.catch\(\(error\)\s*=>\s*toast\(error\.message,\s*true\)\);\s*$/,
  "globalThis.hooks={state,api,renderReport,renderSetup,renderExplore,renderFindings,appendMessage,metricTable,resultTable,esc,renderConnections,updateConnection,setView};",
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
console.log(
  "Static UI contracts and real-report renderer checks passed. No browser visual checks were performed.",
);
