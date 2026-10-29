# Development

Install the application from the repository root with `python -m pip install -e '.[dev]'`. Editable installation makes the `src/` package available to the existing CLI and test commands. Ruff is a development dependency; application runtime dependencies remain empty.

## Source boundaries

- `data/` owns CSV parsing and profiles, value normalization, setup validation, and SQLite persistence.
- `analysis.py` compares snapshots and builds measured reports. `exploration.py` profiles a file through separate field, record, group, relationship, and time-analysis functions.
- `agent/` separates prompts, execution, evidence checks, traces, and optional semantic retrieval.
- `providers.py`, `connections.py`, and `structured.py` own the external model boundary and its response contracts.
- `server/` separates HTTP transport, application operations, report rendering, file watching, and CLI startup.
- `static/` contains the browser interface. `server/templates/` contains standalone export presentation.

Public imports such as `reviso.data.Store`, `reviso.agent.chat`, and `reviso.server.AppServer` remain available. Package initializers only expose that existing API; they do not add wrapper classes or another application layer. HTTP paths, payloads, stored records, and model settings keep their existing behavior.

## Code style

Use descriptive names, grouped imports, clear calls, and one blank line between logical sections. Avoid decorative separators and comments that repeat the code. Extract a helper when it clarifies an existing responsibility; do not introduce interfaces, factories, or service classes without a concrete need.

Run `ruff check src scripts tests` to check imports, unused code, and basic Python correctness. Keep public routes and APIs stable when cleaning up code. The shared model double is in `tests/support/fakes.py`; tests should not import implementation helpers from other test cases.

## Verification

```sh
python -m unittest discover -s tests -v
python scripts/evaluate.py
python scripts/evaluate_single.py
node --check src/reviso/static/app.js
node scripts/check_ui.mjs
```

The two evaluator commands create the input reports used by the static interface check. API tests use temporary loopback servers and databases. The 30 version checks and 12 single-file checks verify deterministic evidence, independently of model generation.

CI installs a regular package so its tests also exercise packaged assets and export templates. A clean checkout uses synthetic housing data when the separately supplied local CSV is unavailable. Runtime databases, keys, generated examples, and evaluation outputs remain outside version control.
