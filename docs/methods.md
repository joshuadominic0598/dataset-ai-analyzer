# Methods and limits

## Import and field profiles

Each version is a complete UTF-8 CSV, with an optional BOM. CSV parsing supports quoted commas and multiline cells. Repeated case-insensitive headers, blank names, inconsistent row widths, malformed CSV, and cells over 20,000 characters are rejected. The initial caps are 20 MB, 100,000 records, and 200 fields per snapshot. There are at most five snapshots per timeline, with no silent removal of older inputs.

Only empty or whitespace-only cells count as missing. `NA`, `None`, `null`, and `0` retain their literal meaning. This is deliberate: the supplied housing description defines `NA` as an absent feature in several fields. The initial version does not offer configurable missing-value tokens.

Field profiles include inferred value type, empty-cell count and share, unique normalized values, leading values, numeric range, mean, and quartiles. Numbers with decimal or exponent notation can still be mathematically integral. Integers within SQLite's 64-bit range are stored exactly; continuous numeric summaries and SQLite REAL values use floating-point precision. Original bytes remain available for exact text recovery.

Numeric comparison normalizes equivalent representations such as `100` and `100.0`. Leading-zero codes such as `001` retain their identity. Outer whitespace is ignored in normalized record comparisons. The app never rewrites the source or cleans records automatically.

## Single-file exploration

One CSV is sufficient and no setup answers are required. An optional question can guide the investigation. The app infers provisional identifiers, a useful measure, recognizable dates, and a possible subject from field names and values. It preserves those choices and their assumptions separately from the original user configuration. The AI uses measured evidence and read-only SQL to investigate further.

All fields in the latest snapshot receive value profiles and suggested scales: identifier, nominal, ordinal, continuous, discrete, datetime, boolean, prose text, or empty. Numeric codes are not assumed to be quantities; leading zeros are preserved. Existing field overrides and ordered-label sequences remain supported through the configuration API; the interface asks only an optional plain-language question. Labels outside that order are flagged. Suggestions do not establish domain semantics.

Numeric measures receive median, observed p95, and robust extreme screening when at least eight numeric values exist. The range is Q1 − 3×IQR to Q3 + 3×IQR; a constant middle uses a fallback spread. Zero-heavy fields with both quartiles at zero are left unscreened and labeled. IDs, categories, and ordinal ratings are excluded from this numeric screening. Rows with several screened extreme measures are counted across the full snapshot and shown with bounded examples. Exact normalized repeated rows and incomplete/repeated selected identities are also measured.

For a selected numeric measure, group summaries cover up to six categorical fields with 2–40 groups each; up to 20 groups are displayed, with coverage reported. Numeric relationship screening examines up to 12 measures and reports up to 12 Pearson associations with absolute correlation at least 0.6 and eight complete pairs. These are descriptive associations, not causal effects or significance tests.

A selected time field supports day/month counts and numeric means. Automatic parsing accepts ISO-like dates; ambiguous dates require an explicit DD/MM/YYYY or MM/DD/YYYY choice. Invalid and empty dates remain visible. Up to the first 60 periods are shown, using the date part as written. The app does not infer expected cadence or label gaps as errors without context. Aggregation suitability and units require user confirmation.

Up to 12 evenly spaced source rows, 12 fields, and 200 characters per value provide record-level examples. This is bounded context rather than a random sample or a claim that the model inspected every record. Models can inspect additional populations with read-only SQL. Deterministic exploration evidence uses E-prefixed IDs; version findings use F; tool evidence uses Q/S.

## Following records

The chosen identity can contain several columns. Analytic Brain groups records by their normalized identity in each version. A complete identity that appears once in each version can be matched directly. Added and removed records are counted through identity membership; edits are differences in shared or linked fields on matched records.

If an identity repeats in either version, all occurrences of that identity are excluded from individual matching in that pair. Blank identities are excluded too. Counts are then explicitly incomplete. The report includes repeated-identity counts and examples, rather than choosing an arbitrary match or silently dropping repeats.

Without a usable identity, a multiset comparison counts matching and unmatched row contents over shared fields. It preserves repeated-row multiplicity. It cannot distinguish an edit from a removal followed by an addition, so those edit counts are not reported.

Newly introduced and removed fields are reported separately; they are outside shared-field edit counts. A record described as unchanged is unchanged **within the comparable fields**, not necessarily across every original byte.

## Names and value types

When fields disappear and others appear, Analytic Brain compares their normalized value multisets. At least 85% overlap relative to the smaller nonempty population creates a possible-link candidate. At least 90% with an unambiguous match creates a provisional alignment. Larger-population coverage is also retained, since an expanded dataset can preserve all earlier values while adding many new ones.

This is a hypothesis, not proof of equivalent meaning or units. The report says so. The agent can investigate the relationship through source examples and SQL. Existing explicit link confirmations or rejections remain supported through the configuration API. Confirmed decisions can link fields even when their values changed substantially. Two old fields cannot be confirmed onto the same new field in a comparison. Ambiguous automatic links are kept separate.

Type changes are inferred from values because CSV has no declared database schema. Whole numbers, continuous numbers, mixed fields, text, and empty fields are distinguished. A separate numeric-format finding catches a move to decimal notation without counting equivalent values as record edits. This initial implementation does not infer every semantic date, unit, or domain type.

## Screening for change

| Signal | Calculation | Interpretation |
| --- | --- | --- |
| Record volume | Percentage change from the previous count; configurable threshold, initially 25% | Large loads and removals need context. Zero-to-nonzero changes have no percentage denominator. |
| Completeness | Change in empty-cell share; configurable threshold, initially 5 percentage points | More empty cells may block use. Filled cells are a potential improvement, subject to value validation. |
| Unusual numeric values | Previous Q1 minus 3×IQR through Q3 plus 3×IQR, with a fallback spread for constant values | A robust screening range, not a validity rule. Requires at least eight numeric observations before comparison. |
| Growing unusual-value count | At least three more unusual values, plus a share increase of at least 0.5 percentage points or 25% of the previous share, whichever is greater | Avoids flagging larger loads merely because they contain more of the same expected extremes. Important fields or share jumps of at least five percentage points receive higher priority. |
| Unfamiliar labels | Labels appearing or disappearing in low-cardinality text fields | May represent legitimate expansion or inconsistent entry. |
| Value mix | Half the sum of absolute category-share changes; signal at 0.15 | Describes composition changes, not correctness. Missing values are excluded from these shares and measured separately. |
| Several unusual fields in a record | Intersection of the first ten unusual examples per field | A bounded investigative sample, not a complete anomaly count or a trained multivariate detector. |

Unusual-value detection is deliberately conservative about meaning. A valid expensive property can fall outside the prior range. Conversely, plausible-looking values can still violate domain rules. Analytic Brain currently provides field prioritization and descriptive context; editable executable domain rules remain future work.

Chosen record-identity fields are excluded from numerical extreme screening. When the previous Q1 and Q3 are identical, flagged values must also fall outside the previous full numeric range. This avoids treating every existing nonzero amenity value in a zero-heavy housing column as an emerging anomaly.

## Text search

Prose-like fields are suggested when their mean nonempty length is at least 50 characters and more than half the values contain spaces. Users can select other text fields too. No embedding runs until the user selects columns, a provider/model, saves the setup, creates a report, and builds its index. External embeddings also require cloud consent; local Ollama embeddings do not.

Search ranks selected cells by cosine similarity. A high score measures relevance in embedding space, not accuracy, truth, sentiment, or a change's business impact. Text search is a retrieval tool for questions, not an automatic semantic quality score.

## AI interpretation

The model sees schema, selected domain context, summary metrics, a bounded selection of prioritized findings, and actual query results. It can propose further checks and plausible explanations. It cannot establish causal history from these snapshots alone.

Analytic Brain validates structured output, tool budgets, reference existence, matching bracket citations, and numerical presence in referenced evidence. Direct decimal rounding of a supplied value is allowed; integer counts must remain exact. IDs and field names are excluded from numerical matches. Numerical presence is a narrow check: the right number can still be attached to the wrong claim. References and human review remain essential. No automatic factuality grade, cleaning action, or model certainty score is fabricated.
