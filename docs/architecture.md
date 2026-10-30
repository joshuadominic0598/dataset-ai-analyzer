# Architecture

Analytic Brain separates immutable inputs, calculated evidence, model interpretation, and operational records. The supplied Spark-based reference code is not imported by the application.

## Runtime

The app uses Python's standard library, SQLite, and plain HTML/CSS/JavaScript. After installing with `python -m pip install -e .`, start it with `analytic-brain` or `python -m reviso`. `ThreadingHTTPServer` serves local assets and JSON endpoints. A three-worker executor runs reports, chats, and embedding builds without tying their completion to an open browser tab. One active investigation is allowed per dataset through the HTTP interface. Upload and setup mutations are blocked while that investigation is active.

The file watcher waits for a stable signature and copies whole CSV bytes into SQLite. It pauses capture while a dataset investigation is running and stops at five versions. It captures local inputs only; it does not trigger cloud work.

## Persistence

| Record | Purpose |
| --- | --- |
| `datasets` | Dataset name and current setup choices |
| `versions` | Ordered version metadata, profiles, original bytes, source digest, and table name |
| `snapshot_*` | Typed, immutable query tables, one per imported version |
| `reports` | Saved configuration, source versions, first-to-latest comparison, neighboring comparisons, findings, and optional interpretation |
| `messages` | Conversation messages linked to a report and a run |
| `vectors` | Selected text cells with version, row, column, provider, model, and embedding |
| `runs` | State, elapsed time, plan actions, tools, provider usage, errors, source fingerprints, and prompt hash |
| `feedback` | User rating and optional note linked to an actual run |

Dataset configuration can change. Reports do not change after publication. An earlier report remains available by ID through `/api/export`; the interface shows the latest report and a stale-input notice when new versions or setup changes exist. A fresh report starts a separate conversation scope. Original CSV downloads return the exact imported bytes.

The database uses WAL mode, a 30-second busy timeout, and a file permission of `0600`. The WAL sidecar, local backups, and operating-system account remain part of the local data boundary. SQLite is not encrypted by this app. No database or provider credentials belong in Git.

## Investigation workflow

1. Parse CSV structure, infer field types, and calculate profiles.
2. Infer provisional identifiers, field meanings, measures, dates, and a subject from the data. Use an optional plain-language question; empty context never blocks analysis. Keep original configuration separate from the inferred analysis choices.
3. Explore the latest file: field meanings, numeric summaries, empty cells, repeated content, unusual records, groups, relationships, and optional time coverage. With two to five files, add neighboring comparisons and a first-to-latest comparison.
4. If AI is selected, send a bounded packet with schema, configuration, summary, prioritized findings, and labeled coverage.
5. Ask the selected model for a brief action plan and at most two SQL queries in low effort or six in high effort. Execute them through the same read-only boundary as interactive queries. Failed queries are passed back as failures.
6. Ask for a structured interpretation. Check evidence IDs and numerical presence before publication. No generated content becomes measured evidence.
7. Chat starts after one upload; if no report exists, it saves a preliminary measured report without requiring setup. It can clarify unknown row meaning or goals, inspect profiles, query records, or use an approved text index. Low effort permits two tools; high permits six. The next call must answer with evidence citations or the run fails explicitly. A rejected report interpretation or final answer gets one extra correction call with available evidence IDs; the rejection is traced and a second validation failure ends the run. Subsequent chat stays pinned to that report until a new report is created.

This is one orchestrated workflow with inspectable stages, rather than several independent agents. It uses no chain-of-thought transcript. Evidence-only mode calculates the same measured report and offers an explicitly labeled evidence lookup.

## Query boundary

Queries run on a new `mode=ro` SQLite connection. A SQLite authorizer permits reads only from allowed snapshot tables and allowlisted functions. Report and chat queries are limited to the versions pinned to that report, even if newer versions exist. Application metadata, other datasets, virtual metadata functions, file attachment, extensions, writes, and expensive unrestricted functions are inaccessible.

The query surface accepts a single `SELECT` or `WITH`, has a two-second execution deadline checked by a progress handler, returns at most 200 records, truncates displayed text cells to 2,000 characters, and rejects result packets over 500,000 characters. SQL and returned results are retained as evidence. Truncation remains visible; a truncated result cannot establish an exhaustive count.

## Model boundary

`src/reviso/providers.py` supports Ollama, OpenAI, Gemini, and Groq. Cloud endpoints are fixed HTTPS addresses. OpenAI uses Responses with `store=false`; Gemini and Groq use compatible chat endpoints. Ollama uses native `/api/chat` on a validated loopback address, with streaming disabled and schema-constrained JSON requested for each workflow stage. Citation arrays are required and reference IDs are restricted to supplied evidence. SQL/profile/semantic actions use separate response shapes; exhausted budgets permit only an answer. Independent backend guards still check references and numeric presence. Low effort disables thinking for models advertising that capability; high enables it. Private reasoning is never consumed or stored. The local default is `qwen3:8b`. Installed text models are discovered with `/api/tags`, and `/api/show` rejects remote aliases before inference. Known embedding-only and cloud-backed entries are excluded from the local chat picker. Loopback calls bypass proxies and reject redirects.

Cloud generation requests have a 35-second timeout; local inference has a 180-second timeout to allow model loading and larger prompts. Model-list requests have a 10-second timeout per page. A rate-limited request gets one bounded retry; there is no cross-provider fallback. Provider error bodies are not copied into traces, since they can echo submitted data.

`src/reviso/connections.py` stores defaults and optional API keys next to the database in `connections.json`, using atomic replacement and mode `0600`. This is a restricted local file, not an encrypted vault. A saved key takes precedence over an environment key; removing the saved copy restores environment fallback. API responses expose model defaults, selected account-tier labels, and credential availability/source, never secret values. Connection settings are pinned in memory for each new run and are not copied into run traces. The CLI evaluator uses environment connections.

The Models screen loads live text-model catalogs from all four providers, offers installed local models, and preserves a custom model-ID option. Free/paid labels are user-provided account context; they do not change billing or prove per-model eligibility. Input data, field names, results, and user context are treated as evidence inside the prompt, never as permission to run arbitrary code. SQL authorization remains the enforcement boundary even if a model disregards the prompt.

The user selects a model context and automatic, low, or high effort. Automatic defaults local/small-context models to low. High requires at least 32k declared context. `src/reviso/investigation.py` assigns explicit evidence, output, query, and tool budgets. Context uses a conservative UTF-8 byte allowance rather than an invented token count. Evidence packets reserve space for history and tool results and label omitted facts. Schema is abbreviated with exact table names; the profile tool can retrieve more field names and profiles. Four recent messages are bounded to 600 characters each. Large tool results use a labeled prefix, while full SQL results remain in traces. Overflow fails explicitly. An additional 100,000-character provider guard remains in place. Actual provider token usage is recorded separately.

`Client.configure_investigation` sets output limits and native reasoning effort only for recognized supporting model families. Ollama's selected `num_ctx` is explicit; thinking requires an advertised capability. No chain-of-thought content is requested in prompts, extracted from responses, or saved. The selected effort and investigation budgets are visible in model usage. Automatic setup choices and their assumptions are recorded in the report and execution trace.

Cloud consent is checked both against the report's saved setup and the dataset's current setup for cloud chat and cloud text indexing. Revoking current consent blocks further cloud investigation of an earlier report. Ollama analysis and Ollama embeddings do not need cloud permission. The deterministic evidence-only report remains usable too.

## Optional text retrieval

Text indexing requires selected columns, an embedding provider/model, and cloud consent when the embedding service is external. Ollama uses local `/api/embed` with truncation disabled; OpenAI and Gemini supply cloud embeddings. Groq can generate answers while a separate connection embeds text. The index is identified by source version, column, row number, provider, and model; each build resumes missing cells and retains completed work.

Vectors are JSON arrays stored in SQLite. Search computes cosine similarity over the selected report version and columns. This deliberately small implementation needs no separate vector service. Index completion is checked against the expected count of nonempty selected cells. The initial limit is 5,000 text cells per report and 6,000 characters per cell; oversized inputs fail instead of being silently excluded.

## HTTP boundary

The application binds to `127.0.0.1`. Allowed Host values include the actual server port. Mutations require a random per-process token in a custom header and reject an external Origin. Fixed-length application/json uploads are capped before body decoding, use a 30-second socket timeout, reject binary controls during CSV parsing, and validate supplied filenames and MIME types. Static assets have a restrictive CSP, no external fonts or scripts, no permissive CORS, no caching, and no framing. Dataset and model content is escaped before display. Downloaded HTML escapes source content and contains no script.

This boundary supports a personal local tool. A hosted service would need authentication, authorization, per-user storage, request limits, a production server, cancellation, and a durable job queue. No hosted security claim is made for this implementation.

## Source organization

The installable package lives under `src/reviso/`. `data/` keeps CSV parsing, setup validation, and the SQLite repository separate. `agent/` keeps prompts, workflow execution, evidence checks, retrieval, and trace persistence separate. `server/` owns HTTP transport, application operations, exports, watching, and startup. Small package initializers preserve the original Python import surface.

HTTP handlers parse requests and delegate dataset views, uploads, report jobs, and queries to application functions. Storage operations remain in `Store`. This keeps responsibilities explicit without additional service classes or dependency-injection frameworks. Report templates and their styles are packaged separately from rendering logic.
