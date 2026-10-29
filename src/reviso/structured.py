"""Local structured output contracts; evidence checks still run after generation."""

def object_schema(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }

def response_schema(context):
    kind = context.get("response_kind")
    text = {"type": "string"}
    ids = [f["id"] for f in context.get("findings", [])]
    ids += [
        r["id"]
        for key in ("query_results", "tool_results")
        for r in context.get(key, [])
        if "error" not in r.get("result", {})
    ]
    reference = {"type": "string", "enum": list(dict.fromkeys(ids))} if ids else text
    references = {"type": "array", "items": reference}
    if kind == "plan":
        return object_schema(
            {
                "plan": {"type": "array", "items": text, "maxItems": 8},
                "queries": {
                    "type": "array",
                    "items": object_schema({"purpose": text, "sql": text}),
                    "maxItems": context["investigation"]["report_queries"],
                },
            }
        )
    if kind == "report":
        return object_schema(
            {
                "summary": text,
                "insights": {
                    "type": "array",
                    "maxItems": 6,
                    "items": object_schema(
                        {
                            "evidence_ids": {**references, "minItems": 1},
                            "interpretation": text,
                            "next_step": text,
                        }
                    ),
                },
            }
        )
    if kind == "chat":
        answer = object_schema({"answer": text, "evidence_ids": references})
        if context.get("remaining_tools") == 0 or context.get("validation_feedback"):
            return answer
        actions = [
            answer,
            object_schema(
                {"tool": {"const": "sql"}, "sql": text, "purpose": text}, ["tool", "sql"]
            ),
            object_schema(
                {
                    "tool": {"const": "profile"},
                    "version": {"type": "integer"},
                    "field_contains": text,
                },
                ["tool", "version"],
            ),
        ]
        if context.get("semantic_index", {}).get("ready"):
            actions.append(
                object_schema(
                    {"tool": {"const": "semantic"}, "query": text, "version": {"type": "integer"}}
                )
            )
        return {"oneOf": actions}
    return "json"
