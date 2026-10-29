"""Bounded report and conversation workflows with observable execution."""

import re
import traceback
from concurrent.futures import ThreadPoolExecutor

from ..analysis import build_report
from ..investigation import bounded_result, settings
from ..providers import Client, ProviderError
from .evidence import (
    _sql,
    checked_answer,
    checked_report,
    packet,
    prepare,
    profile_tool,
)
from .prompts import POLICY, PROMPTS
from .retrieval import build_index, index_status, semantic_search
from .tracing import Trace

EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="reviso")

def generate_report(
    store, dataset_id, trace, provider="offline", model=None, client_factory=Client, options=None
):
    report = build_report(store, dataset_id)
    report["run_id"] = trace.id
    trace.payload["report_id"] = report["id"]
    trace.payload["source_hashes"] = [v["digest"] for v in report["versions"]]
    trace.event(
        "measure",
        {
            "findings": len(report["findings"]),
            "versions": len(report["versions"]),
            "method": report["method_version"],
        },
    )
    if provider != "offline":
        if provider != "ollama" and not report["config"]["cloud_allowed"]:
            raise ValueError("Allow cloud analysis in dataset setup before using a provider.")
        client = client_factory(provider, model, trace.event)
        execution = prepare(client, trace, provider, model, options)
        context = packet(report, execution)
        context["response_kind"] = "plan"
        plan = client.generate(POLICY + PROMPTS["plan"], context)
        if (
            not isinstance(plan.get("plan"), list)
            or any(not isinstance(v, str) or len(v) > 300 for v in plan["plan"])
            or len(plan["plan"]) > 8
        ):
            raise ProviderError("The model returned an invalid investigation plan.")
        queries = plan.get("queries", [])
        if not isinstance(queries, list) or len(queries) > execution["report_queries"]:
            raise ProviderError("The investigation exceeded its query budget.")
        report["plan"].extend(plan["plan"])
        trace.event("plan", {"actions": plan["plan"]})
        results = []
        for i, query in enumerate(queries, 1):
            if not isinstance(query, dict):
                raise ProviderError("The model returned an invalid query action.")
            try:
                result = _sql(
                    store, report, query.get("sql"), trace, query.get("purpose", "Investigate")
                )
            except ValueError as exc:
                result = {"error": str(exc)}
                trace.event("query_rejected", result)
            results.append(
                {
                    "id": "Q" + str(i),
                    "sql": query.get("sql"),
                    "result": bounded_result(
                        result,
                        max(
                            500, int(execution["input_bytes"] * 0.30 / execution["report_queries"])
                        ),
                    ),
                }
            )
        context["query_results"] = results
        context["response_kind"] = "report"
        ai = client.generate(POLICY + PROMPTS["report"], context)
        known = {f["id"]: f for f in context["findings"]}
        known.update({q["id"]: q for q in results if "error" not in q["result"]})
        try:
            checked_report(ai, context, known)
        except ProviderError as exc:
            trace.event("interpretation_rejected", {"reason": str(exc), "recovery_calls": 1})
            context["validation_feedback"] = {
                "error": str(exc),
                "available_evidence_ids": list(known),
                "instruction": "Return a corrected report. Cite only available evidence IDs. Prefer qualitative interpretation; do not invent or round numeric values. Omit unsupported insights.",
            }
            ai = client.generate(POLICY + PROMPTS["report"], context)
            checked_report(ai, context, known)
        ai.update(
            provider=provider,
            model=client.model,
            evidence_checks="Reference IDs and numeric presence checked. Semantic correctness still requires review.",
            query_results=results,
        )
        report["ai"] = ai
    store.save_report(dataset_id, report)
    trace.event("publish", {"report_id": report["id"], "ai": bool(report["ai"])})
    return {"report_id": report["id"]}

def chat(
    store,
    dataset_id,
    question,
    trace,
    provider="offline",
    model=None,
    client_factory=Client,
    options=None,
):
    if not isinstance(question, str) or not question.strip() or len(question) > 4000:
        raise ValueError("Ask a question between 1 and 4,000 characters.")
    report = store.report(dataset_id)
    if not report:
        report = build_report(store, dataset_id, require_setup=False)
        store.save_report(dataset_id, report)
        trace.event(
            "exploratory_context",
            {"report_id": report["id"], "setup_complete": not report.get("draft")},
        )
    trace.payload["report_id"] = report["id"]
    trace.payload["source_hashes"] = [v["digest"] for v in report["versions"]]
    execution = settings(provider, model, options)
    context = packet(report, execution)
    known = {f["id"]: f for f in context["findings"]}
    if provider == "offline":
        if question.strip().lower().startswith(("select ", "with ")):
            evidence = _sql(store, report, question, trace, "Run your read-only query")
            result = {
                "answer": f"The query returned {len(evidence['rows'])} displayed records"
                + (" (limited to 200)." if evidence["truncated"] else ".")
                + " [Q1]",
                "evidence_ids": ["Q1"],
                "evidence": {"Q1": evidence},
                "mode": "local",
            }
        else:
            matches = [
                f
                for f in report["findings"]
                if any(
                    word in (f["title"] + " " + (f["column"] or "") + " " + f["kind"]).lower()
                    for word in re.findall(r"[a-z_]{4,}", question.lower())
                )
            ][:5]
            if not matches:
                matches = report["findings"][:4]
            answer = (
                "Local evidence lookup. Connect a model for conversational investigation.\n\n"
                + "\n\n".join(f"{f['title']}: {f['detail']} [{f['id']}]" for f in matches)
            )
            result = {
                "answer": answer,
                "evidence_ids": [f["id"] for f in matches],
                "evidence": {f["id"]: f for f in matches},
                "mode": "local",
            }
    else:
        if provider != "ollama" and (
            not report["config"]["cloud_allowed"]
            or not store.dataset(dataset_id)["config"]["cloud_allowed"]
        ):
            raise ValueError("Allow cloud analysis in setup before using a provider.")
        client = client_factory(provider, model, trace.event)
        execution = prepare(client, trace, provider, model, options)
        context.update(
            question=question,
            response_kind="chat",
            history=[
                {"role": h["role"], "content": h["content"][:600]}
                for h in store.history(dataset_id, report["id"])
            ][-4:],
            report_interpretation=None,
            semantic_index=index_status(store, report),
            tool_results=[],
        )
        for step in range(execution["chat_tools"] + 1):
            context["remaining_tools"] = max(0, execution["chat_tools"] - step)
            reply = client.generate(POLICY + PROMPTS["chat"], context)
            if "answer" in reply:
                try:
                    ids, evidence = checked_answer(reply, known)
                except ProviderError as exc:
                    # One explicit recovery call; rejected content is never published.
                    trace.event(
                        "answer_rejected",
                        {
                            "reason": str(exc),
                            "recovery_calls": 1,
                            "referenced_ids": reply.get("evidence_ids")
                            if isinstance(reply.get("evidence_ids"), list)
                            else None,
                        },
                    )
                    context["validation_feedback"] = {
                        "error": str(exc),
                        "available_evidence_ids": list(known),
                        "instruction": "Return a final answer with exact available IDs and bracket citations. Use numbers only from cited evidence. If evidence is insufficient, ask a concise clarification. No more tools.",
                    }
                    reply = client.generate(POLICY + PROMPTS["chat"], context)
                    ids, evidence = checked_answer(reply, known)
                result = dict(
                    reply, evidence=evidence, mode="ai", provider=provider, model=client.model
                )
                break
            if step >= execution["chat_tools"]:
                raise ProviderError(
                    "The investigation reached its tool limit without a final answer."
                )
            identifier = ("S" if reply.get("tool") == "semantic" else "Q") + str(step + 1)
            try:
                if reply.get("tool") == "sql":
                    evidence = _sql(
                        store, report, reply.get("sql"), trace, reply.get("purpose", "Investigate")
                    )
                elif reply.get("tool") == "profile":
                    evidence = profile_tool(report, reply)
                elif reply.get("tool") == "semantic":
                    evidence = semantic_search(
                        store,
                        report,
                        reply.get("query"),
                        reply.get("version"),
                        trace,
                        client_factory,
                    )
                else:
                    raise ProviderError("The model requested an unknown tool.")
            except ValueError as exc:
                evidence = {"error": str(exc)}
                trace.event("tool_rejected", {"id": identifier, **evidence})
            evidence = bounded_result(
                evidence, max(500, int(execution["input_bytes"] * 0.25 / execution["chat_tools"]))
            )
            trace.event("tool_context", {"id": identifier, "evidence": evidence})
            if "error" not in evidence:
                known[identifier] = {"request": reply, "result": evidence}
            context["tool_results"].append({"id": identifier, "request": reply, "result": evidence})
    result["report_id"] = report["id"]
    store.message(dataset_id, report["id"], "user", question, trace.id)
    store.message(dataset_id, report["id"], "assistant", result["answer"], trace.id)
    trace.event("answer", {"evidence_ids": result["evidence_ids"]})
    return result

def submit(
    store,
    dataset_id,
    kind,
    provider="offline",
    model=None,
    question="",
    client_factory=Client,
    options=None,
):
    settings(provider, model, options)
    trace = Trace(store, dataset_id, kind, provider, model, question)

    def work():
        try:
            if kind == "report":
                result = generate_report(
                    store, dataset_id, trace, provider, model, client_factory, options
                )
            elif kind == "index":
                result = build_index(store, dataset_id, trace, client_factory)
            else:
                result = chat(
                    store, dataset_id, question, trace, provider, model, client_factory, options
                )
            trace.finish(result)
        except Exception as exc:
            trace.finish(
                error=exc
                if isinstance(exc, ValueError)
                else "The run failed unexpectedly. See the local server console."
            )
            if not isinstance(exc, ValueError):
                traceback.print_exc()

    EXECUTOR.submit(work)
    return trace.id
