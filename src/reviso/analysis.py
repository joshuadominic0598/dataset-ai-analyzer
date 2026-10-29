"""Deterministic evidence. Narratives never own the measurements."""

import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .data import canonical, now, numeric, uid
from .exploration import QUESTIONS, domain_suggestions, explore, infer_role

METHOD_VERSION = "analytic-brain-0.2.0"

def discover(store, dataset_id):
    versions = store.versions(dataset_id)
    if not versions:
        return {"identity_candidates": [], "semantic_candidates": [], "questions": []}
    common = set(versions[0]["columns"]).intersection(*(set(v["columns"]) for v in versions))
    candidates = []
    for c in versions[0]["columns"]:
        if c in common and all(
            v["row_count"] > 0
            and v["profile"][c]["missing"] == 0
            and v["profile"][c]["unique_values"] == v["row_count"]
            for v in versions
        ):
            candidates.append(c)
    candidates.sort(
        key=lambda c: (
            not bool(re.search(r"(^id$|_id$|^id_|identifier|uuid|record.?id)", c, re.I)),
            c,
        )
    )
    semantic = sorted(
        {c for v in versions for c, p in v["profile"].items() if p["semantic_candidate"]}
    )
    config = store.dataset(dataset_id)["config"]
    latest = versions[-1]
    rows = store.rows(dataset_id, latest)
    suggestions = {}
    for col in latest["columns"]:
        role, basis = infer_role(
            col, latest["profile"][col], [r[col] for r in rows if r[col] is not None], config
        )
        suggestions[col] = {"role": role, "basis": basis}
    return {
        "identity_candidates": candidates[:12],
        "semantic_candidates": semantic,
        "field_suggestions": suggestions,
        "domain_questions": QUESTIONS,
        "domain_suggestions": domain_suggestions(versions[-1]["columns"]),
        "follow_up_questions": QUESTIONS[config["dataset_type"]],
        "questions": QUESTIONS[config["dataset_type"]]
        + [
            "Which columns identify a record? A combination is fine.",
            "Which columns matter most, and what would a good or harmful change mean?",
            "May selected evidence be sent to your chosen AI provider?",
        ]
        + (
            [
                "May Analytic Brain embed these description columns for meaning-based search: "
                + ", ".join(semantic)
                + "?"
            ]
            if semantic
            else []
        ),
        "missing_policy": "Only empty or whitespace-only cells count as missing. Values such as NA, None, and 0 keep their original meaning.",
    }

def rename_candidates(previous, current, old_rows, new_rows):
    removed = [c for c in previous["columns"] if c not in current["columns"]]
    added = [c for c in current["columns"] if c not in previous["columns"]]
    scores = []
    for a in removed:
        left = Counter(canonical(r[a]) for r in old_rows if r[a] is not None)
        for b in added:
            right = Counter(canonical(r[b]) for r in new_rows if r[b] is not None)
            if not left or not right:
                continue
            shared = sum((left & right).values())
            overlap = shared / min(sum(left.values()), sum(right.values()))
            similarity = SequenceMatcher(None, a.lower(), b.lower()).ratio()
            if overlap >= 0.85:
                scores.append(
                    {
                        "old": a,
                        "new": b,
                        "overlap": round(overlap, 4),
                        "larger_snapshot_coverage": round(
                            shared / max(sum(left.values()), sum(right.values())), 4
                        ),
                        "name_similarity": round(similarity, 4),
                    }
                )
    # Matching values suggest a rename, but never prove semantic equivalence.
    # Strong unambiguous overlaps are aligned provisionally, and remain labeled a hypothesis.
    mappings = {}
    for s in scores:
        if (
            s["overlap"] >= 0.9
            and sum(x["old"] == s["old"] for x in scores) == 1
            and sum(x["new"] == s["new"] for x in scores) == 1
        ):
            mappings[s["old"]] = s["new"]
    return scores, mappings

def _identity_index(rows, keys):
    groups = defaultdict(list)
    missing = 0
    for index, row in enumerate(rows, 1):
        key = tuple(canonical(row[k]) for k in keys)
        if None in key:
            missing += 1
        else:
            groups[key].append((index, row))
    return groups, missing

def _movement(previous, current, old_rows, new_rows, keys, mapping):
    comparable = [
        (c, mapping.get(c, c))
        for c in previous["columns"]
        if mapping.get(c, c) in current["columns"]
    ]
    if keys and all(
        k in previous["columns"] and mapping.get(k, k) in current["columns"] for k in keys
    ):
        old, old_missing = _identity_index(old_rows, keys)
        new, new_missing = _identity_index(new_rows, [mapping.get(k, k) for k in keys])
        ambiguous = {
            k for k in set(old) | set(new) if len(old.get(k, [])) > 1 or len(new.get(k, [])) > 1
        }
        old_keys, new_keys = set(old) - ambiguous, set(new) - ambiguous
        changed, unchanged, cells, samples = 0, 0, Counter(), []
        for key in sorted(old_keys & new_keys):
            differences = [
                (a, b)
                for a, b in comparable
                if canonical(old[key][0][1][a]) != canonical(new[key][0][1][b])
            ]
            if differences:
                changed += 1
                cells.update(b for a, b in differences)
                if len(samples) < 12:
                    samples.append(
                        {
                            "identity": list(key),
                            "fields": [
                                {
                                    "column": b,
                                    "before": old[key][0][1][a],
                                    "after": new[key][0][1][b],
                                }
                                for a, b in differences[:6]
                            ],
                        }
                    )
            else:
                unchanged += 1
        return {
            "mode": "record_identity",
            "identity": keys,
            "added": len(new_keys - old_keys),
            "removed": len(old_keys - new_keys),
            "changed": changed,
            "unchanged": unchanged,
            "changed_cells": dict(cells),
            "examples": samples,
            "added_examples": [list(k) for k in sorted(new_keys - old_keys)[:12]],
            "removed_examples": [list(k) for k in sorted(old_keys - new_keys)[:12]],
            "ambiguous_identities": len(ambiguous),
            "missing_identity_before": old_missing,
            "missing_identity_after": new_missing,
            "duplicate_identity_rows_before": sum(len(v) for v in old.values() if len(v) > 1),
            "duplicate_identity_rows_after": sum(len(v) for v in new.values() if len(v) > 1),
            "exact": not ambiguous and not old_missing and not new_missing,
            "scope": "Comparable columns only; newly introduced or removed columns are reported separately.",
        }
    if not comparable:
        return {
            "mode": "unavailable",
            "reason": "No comparable columns or usable record identity.",
            "exact": False,
        }
    left = Counter(tuple(canonical(r[a]) for a, _ in comparable) for r in old_rows)
    right = Counter(tuple(canonical(r[b]) for _, b in comparable) for r in new_rows)
    return {
        "mode": "row_content",
        "matched": sum((left & right).values()),
        "unmatched_before": sum((left - right).values()),
        "unmatched_after": sum((right - left).values()),
        "exact": False,
        "scope": "Without a usable identity, edits cannot be distinguished from removals and additions. Compared shared fields with repeated rows counted.",
    }

def compare(previous, current, old_rows, new_rows, config):
    findings = []
    pair = {"from": previous["ordinal"], "to": current["ordinal"]}

    def add(
        kind,
        severity,
        title,
        detail,
        metrics,
        column=None,
        examples=None,
        action="Review against the source and expected behavior.",
        assessment="needs_review",
    ):
        findings.append(
            dict(
                pair,
                kind=kind,
                severity=severity,
                title=title,
                detail=detail,
                metrics=metrics,
                column=column,
                examples=examples or [],
                action=action,
                assessment=assessment,
                important=column in config["important"],
            )
        )

    old_count, new_count = len(old_rows), len(new_rows)
    growth = round(100 * (new_count - old_count) / old_count, 3) if old_count else None
    if old_count != new_count:
        large = growth is None or abs(growth) >= config["growth_threshold"]
        add(
            "record_count",
            "review" if large or new_count < old_count else "info",
            "Record volume changed",
            f"{old_count:,} → {new_count:,} records. Volume alone does not establish whether this change is desirable.",
            {
                "before": old_count,
                "after": new_count,
                "difference": new_count - old_count,
                "change_pct": growth,
                "threshold_pct": config["growth_threshold"],
            },
            action="Confirm the expected load size and whether records were intentionally removed.",
        )
    candidates, mapping = rename_candidates(previous, current, old_rows, new_rows)
    for decision in config.get("rename_decisions", []):
        old, new = decision["old"], decision["new"]
        if (
            old not in previous["columns"]
            or old in current["columns"]
            or new not in current["columns"]
            or new in previous["columns"]
        ):
            continue
        match = next(
            (item for item in candidates if item["old"] == old and item["new"] == new), None
        )
        if not match:
            match = {"old": old, "new": new, "overlap": None, "name_similarity": None}
            candidates.append(match)
        match["user_decision"] = decision["decision"]
        if decision["decision"] == "reject":
            if mapping.get(old) == new:
                del mapping[old]
        else:
            mapping[old] = new
    if len(set(mapping.values())) != len(mapping):
        raise ValueError(
            "Two previous fields link to the same new field. Revise the field-link decisions in setup."
        )
    for item in candidates:
        confirmed = item.get("user_decision") == "confirm"
        rejected = item.get("user_decision") == "reject"
        explanation = (
            "You confirmed these fields have the same meaning; record comparison uses this link."
            if confirmed
            else "You chose to keep these fields separate; record comparison does not link them."
            if rejected
            else "Value overlap suggests the fields may represent the same thing. Matching values do not establish meaning. "
            + (
                "Aligned provisionally because at least 90% of values in the smaller nonempty sample overlap and the match is unambiguous."
                if item["old"] in mapping
                else "Kept separate; confirm their meaning before treating them as the same field."
            )
        )
        add(
            "possible_rename",
            "info",
            f"{'Confirmed field link' if confirmed else 'Fields kept separate' if rejected else 'Possible rename'}: {item['old']} → {item['new']}",
            explanation,
            item,
            column=item["new"],
            action="Update the field-link decision in dataset setup if the meaning or unit differs."
            if confirmed or rejected
            else "Confirm or reject this field link in dataset setup.",
            assessment="user_confirmed"
            if confirmed
            else "user_rejected"
            if rejected
            else "hypothesis",
        )
    added_cols = [
        c for c in current["columns"] if c not in previous["columns"] and c not in mapping.values()
    ]
    removed_cols = [
        c for c in previous["columns"] if c not in current["columns"] and c not in mapping
    ]
    if added_cols or removed_cols:
        add(
            "schema",
            "review" if removed_cols else "info",
            "The set of fields changed",
            "Added and removed fields may reflect a source contract change; possible renames are shown separately.",
            {"added": added_cols, "removed": removed_cols},
            action="Check dependent queries and confirm any intentional removals.",
        )
    movement = _movement(previous, current, old_rows, new_rows, config["identity"], mapping)
    if movement["mode"] == "record_identity":
        add(
            "record_changes",
            "info",
            "Records matched across versions",
            f"{movement['added']:,} added · {movement['removed']:,} removed · {movement['changed']:,} edited · {movement['unchanged']:,} unchanged across comparable fields."
            + (
                " Counts exclude missing or repeated identities and are lower bounds."
                if not movement["exact"]
                else ""
            ),
            movement,
            examples=movement["examples"],
            assessment="observed",
            action="Inspect the changed fields and confirm that record churn was expected.",
        )
        if not movement["exact"]:
            add(
                "identity_conflict",
                "attention",
                "Some records cannot be matched reliably",
                "Missing or repeated identifiers make individual record counts incomplete. Ambiguous identities were excluded, rather than paired arbitrarily.",
                {k: v for k, v in movement.items() if "identity" in k or "ambiguous" in k},
                action="Resolve repeated or blank identifiers, or choose a better combination of columns.",
            )
    else:
        add(
            "record_matching",
            "review",
            "Record matching needs context",
            movement.get("scope", movement.get("reason")),
            movement,
            action="Choose columns that identify the same record over time.",
        )
    common = [
        (c, mapping.get(c, c))
        for c in previous["columns"]
        if mapping.get(c, c) in current["columns"]
    ]
    column_changes = []
    for old_col, col in common:
        p, c = previous["profile"][old_col], current["profile"][col]
        column_changes.append(
            {
                "column": col,
                "previous_column": old_col,
                "before": p,
                "after": c,
                "edited_records": movement.get("changed_cells", {}).get(col),
            }
        )
        if p["kind"] != c["kind"]:
            harmless = {p["kind"], c["kind"]} <= {"integer", "number"}
            add(
                "field_type",
                "info" if harmless else "review",
                f"{col} changed its value type",
                f"Inferred as {p['kind']} before and {c['kind']} now. Numeric widening can be harmless; mixed values may break consumers.",
                {"before": p["kind"], "after": c["kind"]},
                col,
                assessment="observed",
            )
        if p["kind"] in ("integer", "number") and c["kind"] in ("integer", "number"):
            before_decimal = sum(
                bool(r[old_col] and re.search(r"[.eE]", r[old_col])) for r in old_rows
            )
            after_decimal = sum(bool(r[col] and re.search(r"[.eE]", r[col])) for r in new_rows)
            if before_decimal == 0 and after_decimal == new_count and new_count:
                add(
                    "number_format",
                    "info",
                    f"{col} uses a different numeric format",
                    "Numbers now contain decimal or exponent notation. Equivalent numeric values are normalized when records are compared.",
                    {
                        "decimal_notation_before": before_decimal,
                        "decimal_notation_after": after_decimal,
                    },
                    col,
                    assessment="observed",
                )
        missing_delta = round(c["missing_pct"] - p["missing_pct"], 3)
        if abs(missing_delta) >= config["missing_threshold"]:
            improved = missing_delta < 0
            add(
                "missing_values",
                "improved" if improved else "attention",
                f"{col} is {'more complete' if improved else 'missing more values'}",
                f"Empty cells changed from {p['missing']:,} to {c['missing']:,}. This is a {abs(missing_delta):g} percentage-point {'decrease' if improved else 'increase'}.",
                {
                    "before": p["missing"],
                    "after": c["missing"],
                    "before_pct": p["missing_pct"],
                    "after_pct": c["missing_pct"],
                    "percentage_point_change": missing_delta,
                },
                col,
                action="Confirm that the filled values are valid."
                if improved
                else "Inspect the load or transformation producing empty values.",
                assessment="potential_improvement" if improved else "potential_quality_issue",
            )
        if (
            p["numeric_values"] >= 8
            and p["q1"] is not None
            and old_col not in config["identity"]
            and col not in config["identity"]
        ):
            spread = p["q3"] - p["q1"]
            constant_middle = spread == 0
            if spread == 0:
                spread = max(abs(p["q1"]) * 0.1, 1)
            lower, upper = p["q1"] - 3 * spread, p["q3"] + 3 * spread
            lower = max(-sys.float_info.max, lower)
            upper = min(sys.float_info.max, upper)

            def unusual_number(value):
                number = numeric(value)
                # Zero-heavy fields often encode optional amenities. Existing nonzero values are not new anomalies.
                return (
                    number is not None
                    and not lower <= number <= upper
                    and (not constant_middle or not p["min"] <= number <= p["max"])
                )

            old_unusual = sum(unusual_number(r[old_col]) for r in old_rows)
            unusual = [(i, r) for i, r in enumerate(new_rows, 1) if unusual_number(r[col])]
            old_rate, new_rate = old_unusual / max(old_count, 1), len(unusual) / max(new_count, 1)
            if len(unusual) - old_unusual >= 3 and new_rate - old_rate >= max(
                0.005, old_rate * 0.25
            ):
                examples = [
                    {
                        "row": i,
                        "value": r[col],
                        "identity": {k: r.get(mapping.get(k, k)) for k in config["identity"]},
                    }
                    for i, r in unusual[:10]
                ]
                add(
                    "unusual_values",
                    "attention"
                    if col in config["important"] or new_rate - old_rate >= 0.05
                    else "review",
                    f"More unusual values in {col}",
                    "More values fall outside the previous version's typical range. These may be valid extremes or input errors; the range is a screening rule, not a validity rule.",
                    {
                        "before": old_unusual,
                        "after": len(unusual),
                        "increase": len(unusual) - old_unusual,
                        "lower_bound": lower,
                        "upper_bound": upper,
                        "constant_middle": constant_middle,
                        "rule": "previous Q1 − 3×IQR through Q3 + 3×IQR; if Q1=Q3, also outside the previous full range",
                    },
                    col,
                    examples,
                    action="Check the flagged records, their units, and the source system before cleaning anything.",
                    assessment="potential_quality_issue",
                )
        if (
            p["kind"] in ("text", "mixed")
            and p["unique_values"] <= 60
            and c["unique_values"] <= 100
        ):
            old_values = Counter(canonical(r[old_col]) for r in old_rows if r[old_col] is not None)
            new_values = Counter(canonical(r[col]) for r in new_rows if r[col] is not None)
            new_labels = sorted(set(new_values) - set(old_values))
            lost_labels = sorted(set(old_values) - set(new_values))
            total_old, total_new = (
                max(sum(old_values.values()), 1),
                max(sum(new_values.values()), 1),
            )
            shift = 0.5 * sum(
                abs(old_values[k] / total_old - new_values[k] / total_new)
                for k in set(old_values) | set(new_values)
            )
            if new_labels or lost_labels or shift >= 0.15:
                add(
                    "value_mix",
                    "review",
                    f"{col} has a different mix of values",
                    "New labels, disappearing labels, or changed shares may reflect a new population or inconsistent entry.",
                    {
                        "new_labels": new_labels[:30],
                        "disappeared_labels": lost_labels[:30],
                        "share_movement": round(shift, 4),
                        "new_label_counts": {k: new_values[k] for k in new_labels[:30]},
                        "before_top": p["top_values"],
                        "after_top": c["top_values"],
                    },
                    col,
                )
    # A cohort of extremes can matter even when each column alone produces a small signal.
    unusual_fields = [f for f in findings if f["kind"] == "unusual_values"]
    rows_to_fields = defaultdict(list)
    for f in unusual_fields:
        for example in f["examples"]:
            rows_to_fields[example["row"]].append(f["column"])
    cohort = [
        {"row": row, "columns": cols} for row, cols in rows_to_fields.items() if len(cols) >= 2
    ]
    if cohort:
        add(
            "suspicious_records",
            "attention",
            "Some records look unusual in several fields",
            "The same records appear in multiple unusual-value samples. This bounded sample is a lead for investigation, not an exhaustive anomaly count.",
            {"sampled_records": len(cohort), "sample_limit_per_column": 10},
            examples=cohort[:10],
            action="Inspect these records together for a unit, source, or entry-pattern change.",
            assessment="hypothesis",
        )
    return dict(
        pair,
        before=old_count,
        after=new_count,
        growth_pct=growth,
        movement=movement,
        possible_renames=candidates,
        provisional_mappings=mapping,
        column_changes=column_changes,
        findings=findings,
    )

def build_report(store, dataset_id, require_setup=True):
    dataset = store.dataset(dataset_id)
    versions = store.versions(dataset_id)
    if not versions:
        raise ValueError("Upload at least one dataset to analyse.")
    if require_setup and not dataset["config"]["setup_complete"]:
        raise ValueError("Complete the setup questions before creating a report.")
    rows = [store.rows(dataset_id, v) for v in versions]
    comparisons = [
        compare(versions[i - 1], versions[i], rows[i - 1], rows[i], dataset["config"])
        for i in range(1, len(versions))
    ]
    findings = []
    for pair in comparisons:
        for f in pair["findings"]:
            f["id"] = "F" + str(len(findings) + 1).zfill(3)
            findings.append(f)
        del pair["findings"]
    exploration = explore(versions[-1], rows[-1], dataset["config"])
    findings.extend(exploration.pop("findings"))
    priority = {"attention": 0, "review": 1, "improved": 2, "info": 3}
    findings.sort(key=lambda f: (priority[f["severity"]], not f["important"], -f["to"], f["id"]))
    baseline = (
        comparisons[0]
        if len(versions) == 2
        else compare(versions[0], versions[-1], rows[0], rows[-1], dataset["config"])
        if len(versions) > 1
        else None
    )
    baseline = {k: v for k, v in baseline.items() if k != "findings"} if baseline else None
    return {
        "id": uid(),
        "dataset_id": dataset_id,
        "dataset_name": dataset["name"],
        "created_at": now(),
        "method_version": METHOD_VERSION,
        "config": dataset["config"],
        "versions": versions,
        "mode": "exploration" if len(versions) == 1 else "exploration_and_comparison",
        "draft": not dataset["config"]["setup_complete"],
        "comparisons": comparisons,
        "baseline": baseline,
        "findings": findings,
        "exploration": exploration,
        "clarifications": QUESTIONS[dataset["config"]["dataset_type"]],
        "summary": {
            "versions": len(versions),
            "first_records": versions[0]["row_count"],
            "latest_records": versions[-1]["row_count"],
            "attention": sum(f["severity"] == "attention" for f in findings),
            "improvements": sum(f["severity"] == "improved" for f in findings),
            "fields": len(versions[-1]["columns"]),
        },
        "plan": [
            "Clarify dataset type, row meaning, analysis goals, time field, and measures.",
            "Profile all fields and records, suggest measurement scales, and inspect missing values and unusual records.",
            "Investigate group differences, numeric relationships, and time coverage where applicable.",
        ]
        + (
            ["Compare every neighboring version and the first with the latest."]
            if len(versions) > 1
            else []
        )
        + ["Prioritize the selected fields and attach reproducible evidence to each finding."],
        "ai": None,
        "limitations": [
            "Thresholds screen for changes; domain context determines whether a change is desirable.",
            "A possible rename is a hypothesis. Unambiguous value overlap of at least 90% is aligned provisionally.",
            "Record edits cover comparable fields. Missing or repeated identities make record counts incomplete.",
            "Empty cells count as missing. NA and None remain literal values. No automatic cleaning is performed.",
            "Field meanings and ordinal suggestions are hypotheses until confirmed. Associations do not establish causation.",
            "All latest-version records and fields are profiled. Model context, record examples, group summaries, and relationship screening are bounded and labeled.",
        ],
    }
