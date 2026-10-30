"""Bounded evidence packets, tool results, and publication checks."""

import json
import re
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

from ..investigation import settings, size
from ..providers import ProviderError
from .prompts import POLICY, PROMPTS

def packet(report, execution=None):
    execution = execution or settings("ollama")
    budget = execution["input_bytes"]
    # Keep room for history and cumulative tool results as well as instructions.
    limit = int(budget * 0.62)
    config = report.get("analysis_config", report["config"])
    schema, all_columns = [], []
    for version in report["versions"]:
        names = list(
            dict.fromkeys(
                [
                    c
                    for c in config["identity"]
                    + config["important"]
                    + [config.get("target_column"), config.get("date_column")]
                    if c in version["columns"]
                ]
                + version["columns"]
            )
        )
        all_columns.append(names)
        schema.append(
            {
                "version": version["ordinal"],
                "table": version["table"],
                "records": version["row_count"],
                "columns": [],
                "available_fields": len(names),
                "note": "Use profile for omitted fields. Query explicit columns.",
            }
        )
    for index in reversed(range(len(schema))):
        for count in dict.fromkeys((len(all_columns[index]), 12, 3, 1)):
            proposed = [dict(v) for v in schema]
            proposed[index]["columns"] = all_columns[index][:count]
            if size(proposed) <= budget * 0.25:
                schema = proposed
                break
    context = {
        "dataset": report["dataset_name"],
        "report_id": report["id"],
        "schema": schema,
        "config": {
            k: config.get(k)
            for k in (
                "dataset_type",
                "analysis_goal",
                "row_meaning",
                "target_column",
                "date_column",
                "time_grain",
                "identity",
                "important",
                "context",
                "column_roles",
                "category_orders",
            )
        },
        "summary": report["summary"],
        "draft": report.get("draft", False),
        "clarifications": report.get("clarifications", []),
        "analysis_assumptions": [note[:120] for note in report.get("analysis_assumptions", [])[:2]],
        "investigation": execution,
        "findings": [],
        "limitations": [
            "Field meanings are suggestions until confirmed. Associations do not establish causation.",
            "Full-file measurements and complete inference notes are saved. The model sees bounded evidence and abbreviated assumptions; tools can inspect further.",
        ],
    }
    # User context is bounded too; large category-order settings remain in saved evidence.
    if size(context) > limit // 2:
        context["config"] = {
            k: str(config.get(k, ""))[:500]
            for k in (
                "dataset_type",
                "analysis_goal",
                "row_meaning",
                "target_column",
                "date_column",
            )
        }
    findings = report["findings"]
    # Put overview/field meaning/record evidence before large comparison tables.
    preferred = {"dataset_overview": 0, "field_meanings": 1, "record_examples": 2}
    findings = sorted(findings, key=lambda f: preferred.get(f["kind"], 3))
    for f in findings:
        item = {k: v for k, v in f.items() if k not in ("examples", "metrics")}
        item["metrics"] = f["metrics"]
        item["examples"] = f.get("examples", [])[:2]
        if f["kind"] == "field_meanings":
            meanings = f["metrics"]["roles"]
            role_names = list(dict.fromkeys(all_columns[-1] + list(meanings)))[:12]
            item["metrics"] = {
                "role_counts": f["metrics"]["role_counts"],
                "roles": {c: meanings[c] for c in role_names if c in meanings},
                "scope": "First 12 field meanings; use profile for additional fields.",
            }
        if f["kind"] == "record_examples" and execution["effort"] == "low":
            item["examples"] = [
                {
                    "row": example["row"],
                    "values": {
                        c: (v[:120] if isinstance(v, str) else v)
                        for c, v in list(example["values"].items())[:6]
                    },
                }
                for example in item["examples"][:1]
            ]
            item["example_context_scope"] = (
                "One source example, six fields, 120 characters per value. Full samples stay in the report."
            )
        if size(item) > 2500:
            item["metrics"] = {
                "context_note": "Detailed metrics are in the report. Query or use profile for specific fields."
            }
        if size({**context, "findings": context["findings"] + [item]}) < limit:
            context["findings"].append(item)
    context["evidence_coverage"] = {
        "included": len(context["findings"]),
        "available": len(findings),
        "policy": "Priority evidence within the selected conservative context allowance; omitted facts remain queryable.",
    }
    return context

def prepare(client, trace, provider, model, options):
    execution = settings(provider, model, options)
    instruction_bytes = max(len((POLICY + prompt).encode()) for prompt in PROMPTS.values())
    execution["instruction_bytes"] = instruction_bytes
    execution["input_bytes"] -= max(instruction_bytes - 1800, 0)
    if execution["input_bytes"] < 512:
        raise ProviderError(
            "The selected context is too small for the analyst instructions. Select at least 8k context."
        )
    if hasattr(client, "configure_investigation"):
        client.configure_investigation(execution)
    trace.event("investigation_settings", execution)
    return execution

def profile_tool(report, request):
    version = next((v for v in report["versions"] if v["ordinal"] == request.get("version")), None)
    if not version:
        raise ValueError("Choose a version in this report.")
    needle = str(request.get("field_contains", "")).lower()[:100]
    names = [c for c in version["columns"] if needle in c.lower()]
    fields = (
        report.get("exploration", {}).get("fields", {})
        if version == report["versions"][-1]
        else version["profile"]
    )
    return {
        "table": version["table"],
        "matching_fields": len(names),
        "columns": names[:30],
        "profiles": {c: fields.get(c, {}) for c in names[:6]},
        "scope": "First 30 matching names and first six profiles. Narrow field_contains for more.",
    }

def _references(value, known, allow_empty=False):
    ids = value.get("evidence_ids")
    if not isinstance(ids, list):
        raise ProviderError(
            'Return evidence_ids as a JSON array of strings beside answer, for example {"answer":"… [id]","evidence_ids":["id"]}, using actual available IDs.'
        )
    if (not ids and not allow_empty) or any(not isinstance(i, str) or i not in known for i in ids):
        raise ProviderError(
            "The model cited unavailable evidence. No generated claims were published."
        )
    return ids

def _numbers_supported(text, evidence):
    def numbers(s):
        return set(
            re.findall(
                r"(?<![A-Za-z0-9_])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![A-Za-z0-9_])",
                s.replace(",", ""),
            )
        )

    # A narrow guard, not a semantic factuality grade. Labels/IDs are removed from prose first.
    text = re.sub(r"\[(?:F\d+|E\d+|Q\d+|S\d+)\]", "", text)
    available = numbers(json.dumps(evidence, ensure_ascii=False))
    for claimed in numbers(text) - available:
        try:
            value = Decimal(claimed)
            candidates = [Decimal(n) for n in available]
            supported = value in candidates
            # Direct decimal rounding is presentation, not a new measurement.
            # Do not round integer claims, which could invent record counts.
            if not supported and "." in claimed:
                places = len(claimed.rsplit(".", 1)[1])
                quantum = Decimal(1).scaleb(-places)
                for candidate in candidates:
                    try:
                        supported = any(
                            candidate.quantize(quantum, rounding=mode) == value
                            for mode in (ROUND_HALF_UP, ROUND_HALF_EVEN)
                        )
                    except InvalidOperation:
                        continue
                    if supported:
                        break
            if supported:
                continue
        except InvalidOperation:
            pass
        raise ProviderError(
            "The model introduced a number absent from its evidence. No generated claims were published."
        )

def checked_answer(reply, known):
    if not isinstance(reply.get("answer"), str) or len(reply["answer"]) > 12_000:
        raise ProviderError("The model returned an invalid answer.")
    ids = _references(reply, known, allow_empty="?" in reply["answer"])
    if set(re.findall(r"\[((?:F|E|Q|S)\d+)\]", reply["answer"])) != set(ids):
        raise ProviderError("Answer citations did not match the returned evidence.")
    evidence = {i: known[i] for i in ids}
    _numbers_supported(reply["answer"], evidence)
    return ids, evidence

def checked_report(ai, context, known):
    if (
        not isinstance(ai.get("summary"), str)
        or not isinstance(ai.get("insights"), list)
        or len(ai["insights"]) > 6
    ):
        raise ProviderError("The model returned an invalid report structure.")
    _numbers_supported(ai["summary"], context)
    for insight in ai["insights"]:
        if (
            not isinstance(insight, dict)
            or not isinstance(insight.get("interpretation"), str)
            or not isinstance(insight.get("next_step"), str)
        ):
            raise ProviderError("The model returned an invalid insight.")
        ids = _references(insight, known)
        _numbers_supported(
            insight["interpretation"] + " " + insight["next_step"], [known[i] for i in ids]
        )

def _sql(store, report, sql, trace, purpose):
    result = store.query(report["dataset_id"], sql, [v["id"] for v in report["versions"]])
    trace.event("sql", {"purpose": str(purpose)[:250], "sql": sql, "result": result})
    return result
