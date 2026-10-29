"""Explicit investigation budgets, independent of a model's private reasoning."""

from .data import dump

def settings(provider, model=None, options=None):
    options = options or {}
    if not isinstance(options, dict):
        raise ValueError("Investigation settings must be an object.")
    effort = options.get("effort", "auto")
    context = options.get("context_window", 16384 if provider == "ollama" else 32768)
    if effort not in ("auto", "low", "high"):
        raise ValueError("Choose automatic, low, or high investigation effort.")
    if isinstance(context, bool) or not isinstance(context, int) or not 4096 <= context <= 262144:
        raise ValueError(
            "Set a context window between 4,096 and 262,144 tokens supported by your model."
        )
    if effort == "high" and context < 32768:
        raise ValueError("High effort needs a selected context window of at least 32,768 tokens.")
    small = any(
        part in (model or "").lower() for part in ("mini", "nano", "lite", "8b", "7b", "3b", "1b")
    )
    effective = (
        ("low" if provider == "ollama" or context < 32768 or small else "high")
        if effort == "auto"
        else effort
    )
    output = min(2000 if effective == "low" else 8000, context // 4)
    # A conservative UTF-8 byte allowance avoids claiming exact tokenizer accounting.
    return {
        "requested_effort": effort,
        "effort": effective,
        "context_window": context,
        "output_tokens": output,
        "input_bytes": min(90000, context - output - 1800),
        "report_queries": 2 if effective == "low" else 6,
        "chat_tools": 2 if effective == "low" else 6,
        "context_policy": "Conservative UTF-8 bytes; provider token usage is recorded separately. Model capacity is user-declared.",
    }

def size(value):
    return len(dump(value).encode("utf-8"))

def bounded_result(result, budget=1800):
    """Keep tool evidence useful and explicitly mark every omitted row/cell."""
    if size(result) <= budget:
        return result
    if "profiles" in result:
        compact = {k: v for k, v in result.items() if k != "profiles"}
        compact["profiles"] = {}
        compact["context_note"] = (
            "Field profiles abbreviated for context. Narrow field_contains for detailed values."
        )
        for name, profile in result["profiles"].items():
            item = {
                k: v
                for k, v in profile.items()
                if k
                in (
                    "kind",
                    "role",
                    "missing",
                    "missing_pct",
                    "min",
                    "max",
                    "median",
                    "unique_values",
                )
            }
            candidate = {**compact, "profiles": {**compact["profiles"], name: item}}
            if size(candidate) > budget:
                break
            compact = candidate
        if size(compact) <= budget:
            return compact
    rows_key = "rows" if "rows" in result else "matches" if "matches" in result else None
    if rows_key:
        compact = {k: v for k, v in result.items() if k != rows_key}
        compact[rows_key] = []
        compact["context_note"] = (
            "Only a prefix fits the model context; full returned evidence is saved in run activity."
        )
        for row in result[rows_key]:
            candidate = {**compact, rows_key: compact[rows_key] + [row]}
            if size(candidate) > budget:
                break
            compact = candidate
        if size(compact) <= budget:
            return compact
    return {
        "context_note": "Result exceeds the context allowance. Query fewer explicit columns or use an aggregate. Full result is saved in run activity."
    }
