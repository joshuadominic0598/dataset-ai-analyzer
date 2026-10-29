"""Reproducible exploratory analysis of columns, records, and relationships."""

import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import median

from .data import canonical, numeric, profile

DATASET_TYPES = {
    "unknown",
    "time_series",
    "orders",
    "doctors",
    "claims",
    "prices",
    "supply_chain",
    "other",
}
ROLES = {
    "identifier",
    "nominal",
    "ordinal",
    "continuous",
    "discrete",
    "datetime",
    "boolean",
    "text",
    "empty",
}
DATE_FORMATS = {"auto", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"}
QUESTIONS = {
    "unknown": [
        "What kind of data is this, and what does one row represent?",
        "What decision or question should the analysis help with?",
    ],
    "time_series": [
        "Which field is the observation time, and which measure should we track?",
        "Is each row an event or a regular observation? What frequency and timezone do you expect?",
    ],
    "orders": [
        "Does a row represent an order, an item, or a payment?",
        "Which amount is meaningful: gross, net, quantity, or margin? Are cancellations and returns included?",
    ],
    "doctors": [
        "Does a row represent a doctor, a location, or a doctor-location combination?",
        "Should we focus on completeness, specialty coverage, geography, or repeated provider records?",
    ],
    "claims": [
        "Does a row represent a claim, a service line, or a payment?",
        "Which amount and status matter, and what counts as a repeated or incomplete claim?",
    ],
    "prices": [
        "What is being priced, in which currency and unit?",
        "Should we investigate price variation, extreme values, missing prices, or change over time?",
    ],
    "supply_chain": [
        "Does a row represent an item, shipment, supplier, or inventory observation?",
        "Are lead time, stock levels, unit costs, or supplier differences the main concern?",
    ],
    "other": [
        "What does one row represent, and which fields identify it?",
        "Which outcome or decision matters, and are there known value rules or category orders?",
    ],
}

def date_value(value, fmt="auto"):
    if not value:
        return None
    try:
        text = str(value).strip()
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            if fmt == "auto"
            else datetime.strptime(text, fmt)
        )
    except (ValueError, TypeError, OverflowError):
        return None

def infer_role(column, facts, values, config):
    override = config.get("column_roles", {}).get(column)
    if override:
        return override, "Chosen by the user."
    if column in config.get("identity", []) or re.search(
        r"(^id$|_id$|^id_|identifier|uuid|^npi$)", column, re.I
    ):
        return (
            "identifier",
            "Identifier name or selected record identity; confirm that this is not a measure.",
        )
    if not values:
        return "empty", "No nonempty values."
    if column == config.get("date_column") or (
        facts["kind"] == "text"
        and sum(date_value(v) is not None for v in values) / len(values) >= 0.9
    ):
        return (
            "datetime",
            "Selected time field or at least 90% ISO-like dates; ambiguous date formats require confirmation.",
        )
    if column in config.get("category_orders", {}):
        return "ordinal", "An explicit category order was supplied by the user."
    labels = {str(v).strip().lower() for v in values}
    if labels <= {"true", "false"} or labels <= {"yes", "no"} or labels <= {"y", "n"}:
        return "boolean", "Boolean-like labels; confirm their meaning."
    known_orders = [
        {"low", "medium", "high"},
        {"poor", "fair", "good", "excellent"},
        {"po", "fa", "ta", "gd", "ex"},
    ]
    if len(labels) > 1 and any(labels <= order for order in known_orders):
        return (
            "ordinal",
            "Labels suggest an order. Confirm the order before ranking or aggregating them.",
        )
    if facts["kind"] in ("integer", "number"):
        if facts["unique_values"] <= 20 and re.search(
            r"qual|cond|rating|rank|grade|level|priority", column, re.I
        ):
            return (
                "ordinal",
                "A numeric rating or level is suggested by the name; ordering still needs domain confirmation.",
            )
        if facts["unique_values"] <= 40 and re.search(
            r"code|class|status|type|month|year|zip|postal", column, re.I
        ):
            return "nominal", "Numeric codes may name categories rather than measure a quantity."
        if re.search(
            r"price|amount|cost|revenue|margin|weight|height|length|area|temperature|speed|distance|salary|balance|income",
            column,
            re.I,
        ):
            return (
                "continuous",
                "The name suggests a monetary or measured quantity, even when stored as whole numbers. Confirm units and scale.",
            )
        return (
            ("discrete" if facts["kind"] == "integer" else "continuous"),
            "Numeric values; whole-number storage alone does not establish the real-world measurement scale.",
        )
    if facts["semantic_candidate"] or facts["unique_values"] > max(40, len(values) * 0.5):
        return "text", "Prose-like or high-cardinality text."
    return "nominal", "Category labels with no established order."

def domain_suggestions(columns):
    names = " ".join(columns).lower()
    cues = {
        "orders": ("order", "purchase", "customer", "invoice"),
        "doctors": ("doctor", "provider", "npi", "specialty"),
        "claims": ("claim", "payer", "billed", "allowed"),
        "prices": ("price", "saleamount", "valuation"),
        "supply_chain": ("supplier", "inventory", "shipment", "leadtime", "stock"),
        "time_series": ("timestamp", "datetime", "observation"),
    }
    ranked = [(sum(cue in names for cue in words), kind) for kind, words in cues.items()]
    return [
        {"type": kind, "basis": "Field names suggest this subject; please confirm."}
        for score, kind in sorted(ranked, reverse=True)
        if score
    ][:3]

def _profile_fields(columns, rows, facts, config, add):
    roles, fields = {}, {}
    numeric_cache = {}
    unusual_records = defaultdict(list)
    for col in columns:
        p = facts[col]
        values = [r[col] for r in rows if r[col] is not None]
        role, basis = infer_role(col, p, values, config)
        roles[col] = role
        numbers = sorted(v for v in (numeric(value) for value in values) if v is not None)
        record = dict(
            p,
            role=role,
            role_basis=basis,
            role_confirmed=col in config.get("column_roles", {})
            or col in config.get("category_orders", {}),
            median=median(numbers) if numbers else None,
            p95=numbers[int((len(numbers) - 1) * 0.95)] if numbers else None,
            category_order=config.get("category_orders", {}).get(col, []),
        )
        fields[col] = record
        order = config.get("category_orders", {}).get(col)
        if order:
            unknown = Counter(str(v) for v in values if str(v) not in order)
            if unknown:
                add(
                    "unordered_labels",
                    "review",
                    f"{col} contains labels outside your chosen order",
                    "Some observed labels cannot be ranked using the supplied category order.",
                    {
                        "unranked_records": sum(unknown.values()),
                        "unranked_labels": dict(unknown.most_common(20)),
                        "category_order": order,
                    },
                    col,
                    action="Confirm spelling and the intended order before comparing ranks.",
                )
        if p["missing"]:
            add(
                "missing_current",
                "attention" if col in config["important"] or p["missing_pct"] >= 20 else "review",
                f"{col} has empty values",
                "Empty cells may be acceptable or may block analysis, depending on what this field represents.",
                {"missing": p["missing"], "missing_pct": p["missing_pct"], "records": len(rows)},
                col,
                [{"row": i} for i, r in enumerate(rows, 1) if r[col] is None][:8],
                action="Confirm whether this field is required and inspect the affected records.",
                assessment="potential_quality_issue",
            )
        if values and p["unique_values"] == 1:
            add(
                "constant_field",
                "info",
                f"{col} has one observed value",
                "This field does not separate records within the current snapshot. It may still be useful context.",
                {"value": str(values[0])[:200], "nonempty_records": len(values)},
                col,
            )
        if role in ("continuous", "discrete") and len(numbers) >= 8:
            numeric_cache[col] = [numeric(r[col]) for r in rows]
            spread = p["q3"] - p["q1"]
            if spread == 0 and p["q1"] == 0:
                record["outlier_note"] = (
                    "The middle range collapses at zero. No automatic extreme-value count is assigned to this zero-heavy field."
                )
                continue
            spread = spread or max(abs(p["q1"]) * 0.1, 1)
            lower, upper = (
                max(-1.7976931348623157e308, p["q1"] - 3 * spread),
                min(1.7976931348623157e308, p["q3"] + 3 * spread),
            )
            unusual = [
                (i, r[col])
                for i, r in enumerate(rows, 1)
                if numeric(r[col]) is not None and not lower <= numeric(r[col]) <= upper
            ]
            for i, value in unusual:
                unusual_records[i].append({"field": col, "value": value})
            record.update(unusual_values=len(unusual), lower_screen=lower, upper_screen=upper)
            if unusual:
                add(
                    "unusual_current",
                    "review",
                    f"{col} has values worth inspecting",
                    "Values outside the current robust screening range are leads for investigation, not proven input errors.",
                    {
                        "count": len(unusual),
                        "lower_bound": lower,
                        "upper_bound": upper,
                        "rule": "Q1 − 3×IQR through Q3 + 3×IQR; fallback spread for a constant middle",
                    },
                    col,
                    [
                        {
                            "row": i,
                            "value": value,
                            "identity": {
                                key: rows[i - 1][key]
                                for key in config.get("identity", [])
                                if key in columns
                            },
                        }
                        for i, value in unusual[:8]
                    ],
                    action="Check units and source records before treating these values as errors.",
                    assessment="potential_quality_issue",
                )
    multiple = [(i, values) for i, values in unusual_records.items() if len(values) > 1]
    if multiple:
        add(
            "record_screen",
            "review",
            "Some records contain several unusual measures",
            "The same source rows exceed the screening range in more than one numeric measure. This is a descriptive screen, not a trained anomaly detector.",
            {"records": len(multiple), "minimum_unusual_fields": 2},
            examples=[{"row": i, "fields": values[:8]} for i, values in multiple[:8]],
            action="Inspect these rows together for unit differences, mixed populations, or entry issues.",
        )
    return fields, roles, numeric_cache

def _inspect_repetitions(columns, rows, config, add):
    duplicates = defaultdict(list)
    for i, row in enumerate(rows, 1):
        duplicates[tuple(canonical(row[c]) for c in columns)].append(i)
    repeated = [indices for indices in duplicates.values() if len(indices) > 1]
    if repeated:
        add(
            "repeated_records",
            "review",
            "Some complete records repeat",
            "Repeated content may represent repeated events or duplicate ingestion. Row meaning determines which interpretation is appropriate.",
            {
                "groups": len(repeated),
                "records_in_groups": sum(map(len, repeated)),
                "extra_copies": sum(len(g) - 1 for g in repeated),
            },
            examples=[{"rows": group[:8], "occurrences": len(group)} for group in repeated[:8]],
            action="Confirm whether repeated events are legitimate before removing anything.",
        )
    keys = config.get("identity", [])
    if keys and all(c in columns for c in keys):
        groups = Counter(
            tuple(canonical(r[c]) for c in keys)
            for r in rows
            if all(r[c] is not None for c in keys)
        )
        missing = sum(any(r[c] is None for c in keys) for r in rows)
        repeated_keys = sum(n > 1 for n in groups.values())
        if missing or repeated_keys:
            add(
                "identity_current",
                "attention",
                "The chosen record identity is not complete and unique",
                "A row may represent a more detailed event than the selected identifier. Confirm row meaning before treating repeated identifiers as errors.",
                {
                    "identity": keys,
                    "missing_records": missing,
                    "repeated_identities": repeated_keys,
                },
                action="Choose an appropriate combination of identifying fields or document legitimate repetitions.",
            )

def _summarize_groups(columns, rows, roles, config, add):
    target = config.get("target_column", "")
    group_summaries = []
    if target in columns and roles[target] in ("continuous", "discrete"):
        group_columns = [
            c
            for c in config["important"] + columns
            if c != target and roles.get(c) in ("nominal", "ordinal", "boolean")
        ]
        for col in list(dict.fromkeys(group_columns))[:6]:
            groups = defaultdict(list)
            for row in rows:
                number = numeric(row[target])
                if row[col] is not None and number is not None:
                    groups[str(row[col])].append(number)
            if 1 < len(groups) <= 40:
                result = [
                    {
                        "group": name,
                        "records": len(v),
                        "mean": sum(n / len(v) for n in v),
                        "median": median(v),
                    }
                    for name, v in sorted(groups.items(), key=lambda item: -len(item[1]))[:20]
                ]
                group_summaries.append(
                    {
                        "field": col,
                        "measure": target,
                        "groups": result,
                        "available_groups": len(groups),
                        "shown_groups": len(result),
                    }
                )
                add(
                    "group_comparison",
                    "info",
                    f"{target} by {col}",
                    "Group sizes and values may help explain variation. Differences do not establish causation, and small groups need caution.",
                    group_summaries[-1],
                    col,
                    action="Inspect group sizes and context before drawing conclusions.",
                )
    return group_summaries

def _find_relationships(columns, numeric_cache, config, add):
    target = config.get("target_column", "")
    numeric_columns = list(dict.fromkeys([target] + config["important"] + columns))
    numeric_columns = [c for c in numeric_columns if c in numeric_cache][:12]
    associations = []
    for j, a in enumerate(numeric_columns):
        for b in numeric_columns[j + 1 :]:
            paired = [
                (x, y)
                for x, y in zip(numeric_cache[a], numeric_cache[b])
                if x is not None and y is not None
            ]
            if len(paired) < 8:
                continue
            sx = max(abs(x) for x, y in paired) or 1
            sy = max(abs(y) for x, y in paired) or 1
            xs = [x / sx for x, y in paired]
            ys = [y / sy for x, y in paired]
            mx = sum(xs) / len(xs)
            my = sum(ys) / len(ys)
            xx = sum((x - mx) ** 2 for x in xs)
            yy = sum((y - my) ** 2 for y in ys)
            if xx and yy:
                correlation = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)
                if abs(correlation) >= 0.6:
                    associations.append(
                        {
                            "left": a,
                            "right": b,
                            "pearson_r": round(max(-1, min(1, correlation)), 4),
                            "paired_records": len(paired),
                        }
                    )
    associations.sort(key=lambda item: -abs(item["pearson_r"]))
    if associations:
        add(
            "numeric_relationships",
            "info",
            "Some numeric fields move together",
            "These pairwise associations use complete numeric pairs. They do not prove causation and can be influenced by extreme values.",
            {
                "relationships": associations[:12],
                "numeric_fields_screened": len(numeric_columns),
                "field_limit": 12,
            },
            action="Inspect scatter patterns, extreme records, and domain explanations.",
        )
    return associations

def _summarize_time(columns, rows, facts, config, add):
    target = config.get("target_column", "")
    time_summary = None
    date_col = config.get("date_column", "")
    if date_col in columns:
        parsed = [(date_value(r[date_col], config.get("date_format", "auto")), r) for r in rows]
        valid = [(date, row) for date, row in parsed if date is not None]
        buckets = defaultdict(list)
        grain = config.get("time_grain", "month")
        for date, row in valid:
            bucket = date.strftime("%Y-%m-%d" if grain == "day" else "%Y-%m")
            buckets[bucket].append(numeric(row.get(target)))
        periods = []
        for bucket, values in sorted(buckets.items())[:60]:
            numbers = [v for v in values if v is not None]
            periods.append(
                {
                    "period": bucket,
                    "records": len(values),
                    "numeric_records": len(numbers),
                    "mean": sum(v / len(numbers) for v in numbers) if numbers else None,
                }
            )
        time_summary = {
            "field": date_col,
            "measure": target or None,
            "grain": grain,
            "valid_dates": len(valid),
            "missing_dates": facts[date_col]["missing"],
            "invalid_dates": len(rows) - len(valid) - facts[date_col]["missing"],
            "periods": periods,
            "available_periods": len(buckets),
            "scope": "First 60 periods, using the date part as written. Gaps are not errors without an expected observation schedule.",
        }
        add(
            "time_overview",
            "info",
            "Coverage over time",
            "The selected time field supports a period-level view. Confirm the time grain, row meaning, and the measure's aggregation semantics.",
            time_summary,
            date_col,
        )
    return time_summary

def _sample_records(columns, rows, config, add):
    keys = config.get("identity", [])
    target = config.get("target_column", "")
    selected_columns = list(
        dict.fromkeys(keys + config["important"] + ([target] if target else []) + columns)
    )[:12]
    positions = sorted(
        {
            round(i * (len(rows) - 1) / min(11, max(len(rows) - 1, 1)))
            for i in range(min(12, len(rows)))
        }
    )
    samples = [
        {
            "row": i + 1,
            "values": {
                c: (str(rows[i][c])[:200] if rows[i][c] is not None else None)
                for c in selected_columns
                if c in columns
            },
        }
        for i in positions
    ]
    add(
        "record_examples",
        "info",
        "Source record examples",
        "Evenly spaced source rows provide bounded record-level context. They are not a random or exhaustive sample.",
        {
            "sampled_records": len(samples),
            "total_records": len(rows),
            "sampled_fields": selected_columns,
            "total_fields": len(columns),
        },
        examples=samples,
        action="Use read-only SQL to inspect any specific population or suspicious records.",
    )
    return samples

def explore(version, rows, config):
    columns = version["columns"]
    facts = profile(columns, rows)
    findings = []
    ordinal = version["ordinal"]

    def add(
        kind,
        severity,
        title,
        detail,
        metrics,
        column=None,
        examples=None,
        action="Confirm the meaning and investigate the source.",
        assessment="observed",
    ):
        findings.append(
            {
                "id": "E" + str(len(findings) + 1).zfill(3),
                "kind": kind,
                "severity": severity,
                "title": title,
                "detail": detail,
                "metrics": metrics,
                "column": column,
                "examples": examples or [],
                "action": action,
                "assessment": assessment,
                "important": column in config["important"],
                "from": ordinal,
                "to": ordinal,
                "scope": "snapshot",
            }
        )

    add(
        "dataset_overview",
        "info",
        "The dataset at a glance",
        "The full snapshot was profiled. Record examples are bounded and do not represent an exhaustive review by the model.",
        {"records": len(rows), "fields": len(columns), "version": ordinal},
        action="Confirm what a row represents and the question you want answered.",
    )
    fields, roles, numeric_cache = _profile_fields(columns, rows, facts, config, add)
    add(
        "field_meanings",
        "info",
        "Suggested field meanings",
        "Categories, ratings, identifiers, dates, and quantities need different analysis. Suggestions can be corrected in dataset setup.",
        {"role_counts": dict(Counter(roles.values())), "roles": roles},
        action="Confirm nominal and ordinal meanings, numeric codes, and any ordered labels.",
        assessment="hypothesis",
    )
    _inspect_repetitions(columns, rows, config, add)
    group_summaries = _summarize_groups(columns, rows, roles, config, add)
    associations = _find_relationships(columns, numeric_cache, config, add)
    time_summary = _summarize_time(columns, rows, facts, config, add)
    samples = _sample_records(columns, rows, config, add)
    return {
        "version": ordinal,
        "records": len(rows),
        "fields": fields,
        "role_counts": dict(Counter(roles.values())),
        "record_samples": samples,
        "group_summaries": group_summaries,
        "relationships": associations[:12],
        "time_summary": time_summary,
        "coverage": {
            "profiled_records": len(rows),
            "profiled_fields": len(columns),
            "record_sample_limit": 12,
            "relationship_field_limit": 12,
        },
        "findings": findings,
    }
