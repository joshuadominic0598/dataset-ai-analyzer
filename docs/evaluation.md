# Evaluation and monitoring

Analytic Brain keeps measurement correctness, model execution, evidence checks, and human judgment separate.

## Independent expected changes

`reviso/demo.py` creates controlled changes to the supplied house-price sample, with a generated-data fallback for a clean checkout. The expected-change manifest is authored alongside the mutations rather than derived from detector output.

| Transition | Injected changes |
| --- | --- |
| V1 → V2 | 60 additions; 600 retained records unchanged |
| V2 → V3 | 50 additions, 10 removals, 40 blank prices, 12 extreme price/area records, 10 changed neighborhood labels, and two repeated identities |
| V3 → V4 | Repair changed values, remove extra repeated occurrences, rename SalePrice to SaleAmount, and write prices with decimal notation |
| V4 → V5 | 300 additions and a SourceBatch field; 700 retained records unchanged in comparable fields |

V3 has 702 rows, but only 698 can participate in unambiguous current-identity counts. The two repeated identities affect both versions' comparability. The expected 62 changed records and 586 unchanged records are lower bounds, with 50 unambiguous additions and 10 removals. The benchmark deliberately verifies this limit instead of assuming uniqueness.

Run `python3 scripts/make_demo.py` to write editable sample CSVs and `expected_changes.json`. Run `python3 scripts/evaluate.py` for 30 checks on exact counts, matching completeness, and required signal presence. The report also includes incidental changes from new source populations; the benchmark is not a false-positive-rate estimate.

## Automated tests

`python3 -m unittest discover -s tests -v` tests parsing, original-byte preservation, identifier ambiguity, numeric equivalence, missing-value semantics, growth, renames, independent housing expectations, SQLite authorization, result limits, cross-dataset isolation, future-version isolation, provider contracts, model output guards, text indexing, quota handling, API flows, exports, feedback, and file watching.

The provider tests use mocked responses. They validate endpoint shapes and failure handling without spending tokens or claiming a live connection. HTTP integration tests use a temporary loopback server and temporary database. If the execution environment prohibits sockets, those tests are explicitly skipped; a skipped test is not a pass.

The optional `scripts/check_ui.mjs` checks static element contracts and executes the actual renderer against the evaluated report using a minimal DOM stub. It verifies escaping and expected rendering output. It is not a browser engine and does not establish responsive layout or interaction correctness. Browser visual verification remains a separate check.

## Live model experiments

Configure a key and run:

```sh
python3 scripts/evaluate.py --provider gemini --model YOUR_MODEL_ID --output outputs/gemini-run.json
```

The same command accepts OpenAI, Groq, or local Ollama (`--provider ollama --model qwen3:8b`). It generates an AI report, then asks four questions about additions, prices, renames, and identity problems. It records answers, cited evidence, source hashes, prompt hash, tool results, token usage when supplied, elapsed times, and failures. Human semantic review remains pending. It does not assign itself a passing factuality score. Any failed answer execution gives the command a nonzero exit status while preserving the result file.

For a useful comparison, hold source snapshots, configuration, manifest, prompt hash, and question order constant. Compare completed answers for factual support, useful clarification, appropriate uncertainty, and actionable next steps. A refusal, quota error, malformed answer, or exhausted tool budget stays visible as a failure. The app does not average failures into a misleading quality grade.

## Local operational traces

Run activity shows the latest 50 runs in the selected dataset, including report generation, chat, SQL, and indexing. Every run has a durable ID and lifecycle state. A server restart marks unfinished runs as failed rather than leaving them permanently running.

Events record observable actions: measurements, model calls, plans, SQL and results, text indexing progress, semantic search results, reference checks through successful completion, publication, and failure. Source fingerprints and the shared prompt hash make the inputs identifiable. Prompt definitions live in `reviso/agent.py`; private reasoning is neither requested nor stored.

Provider-reported token counts are retained without inventing usage when absent. Failed HTTP attempts have failure events. Cost remains unknown until pricing and account tier are explicitly implemented; choosing Gemini or Groq does not make an inferred zero-cost claim. No telemetry is sent to an external monitoring service.

User feedback is stored against an actual run. It supports review without silently changing an answer or its evidence. Historical messages link back to the report and the run that generated them, so cited evidence can be inspected again.

External tracing, configurable per-model pricing, a versioned question editor, semantic grading, and experiment-comparison UI are future work. The current foundation provides exact measurement tests and durable execution evidence for those additions.

## Single-file analyst benchmark

`python3 scripts/evaluate_single.py` checks 12 independently specified facts from a 30-record property sample. Expected values are authored separately from detector outputs: 30 records, seven fields, one missing description, one unusual price, median 155, ordinal quality, nominal region, two regional groups, 30 valid dates, perfect sample price-area association, and one row with several unusual measures. The fixture is deliberately simple and does not establish performance on arbitrary real datasets.

Use `--provider ollama --model qwen3:8b --effort low` for a live report plus two SQL/evidence conversations. `--effort high --context 32768` permits a deeper investigation on a supporting model. The JSON artifact retains source inputs, report, run settings, plan actions, queries, provider usage, answers, and failures. Execution and citation/numeric guards do not establish semantic correctness. Cloud paths require credentials and explicit selection; local runs never fall back to them.

The unit/API suite also checks chat before setup, editable scale suggestions, explicit date interpretation, upload protocol, effort validation, native capability controls, local response-schema contracts, direct decimal rounding without invented integer counts, and oversized context rejection. Static UI renderer checks cover the one-file report, field controls, model controls, escaping, and agent launcher contract. They do not establish browser layout or responsive visual correctness.
