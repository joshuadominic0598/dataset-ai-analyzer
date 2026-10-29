"""Application operations shared by the local HTTP endpoints."""

import base64

from ..agent import Trace, index_status, submit
from ..analysis import discover

def dataset_view(store, dataset_id):
    dataset = store.dataset(dataset_id)
    versions = store.versions(dataset_id)
    report = store.report(dataset_id)
    stale = bool(
        report
        and (
            [version["digest"] for version in versions]
            != [version["digest"] for version in report["versions"]]
            or dataset["config"] != report["config"]
        )
    )
    return {
        "dataset": dataset,
        "versions": versions,
        "discovery": discover(store, dataset_id),
        "report": report,
        "report_stale": stale,
        "index": index_status(store, report) if report else None,
    }

def conversation_history(store, dataset_id):
    report = store.report(dataset_id)
    messages = store.history(dataset_id, report["id"]) if report else []
    for message in messages:
        if message["role"] == "assistant":
            result = store.run(message["run_id"])["payload"].get("result") or {}
            message["evidence"] = result.get("evidence")
    return {"messages": messages}

def ensure_dataset_idle(server, dataset_id):
    with server.jobs_lock:
        active = server.active_jobs.get(dataset_id)
        if active and server.store.run(active)["status"] == "running":
            raise ValueError("Wait for the active investigation before changing its data or setup.")

def decode_upload(body):
    filename = body.get("filename")
    if filename is not None and (
        not isinstance(filename, str)
        or not filename.lower().endswith(".csv")
        or any(character in filename for character in ("/", "\\", "\x00"))
    ):
        raise ValueError("Choose a CSV file with a plain filename.")
    if body.get("content_type", "") not in (
        "",
        "text/csv",
        "application/csv",
        "text/plain",
        "application/vnd.ms-excel",
        "application/octet-stream",
    ):
        raise ValueError("Choose a CSV text file. The file content must also pass CSV validation.")
    try:
        return base64.b64decode(body.get("content", ""), validate=True)
    except (ValueError, TypeError):
        raise ValueError("The uploaded file could not be decoded.") from None

def import_demo(store, dataset_id):
    from ..demo import load_demo

    if store.versions(dataset_id):
        raise ValueError("Load the demo into a new, empty dataset timeline.")
    load_demo(store, dataset_id)
    return {"loaded": True}

def start_investigation(server, dataset, path, body):
    provider, model = body.get("provider", "offline"), body.get("model")
    if provider not in ("offline", "ollama", "openai", "gemini", "groq"):
        raise ValueError("Choose a supported provider.")
    if provider != "offline":
        model = model or server.connections.resolve(provider)["model"]
    if provider not in ("offline", "ollama") and not dataset["config"]["cloud_allowed"]:
        raise ValueError("Allow cloud analysis in setup before using a provider.")
    kind = {"/api/report": "report", "/api/chat": "chat", "/api/index": "index"}[path]
    dataset_id = dataset["id"]
    with server.jobs_lock:
        previous = server.active_jobs.get(dataset_id)
        if previous and server.store.run(previous)["status"] == "running":
            raise ValueError(
                "This dataset already has a running investigation. Wait for it to finish."
            )
        identifier = submit(
            server.store,
            dataset_id,
            kind,
            provider,
            model,
            body.get("question", ""),
            server.connections.client_factory(),
            body.get("investigation"),
        )
        server.active_jobs[dataset_id] = identifier
    return {"run_id": identifier}

def query_data(store, dataset_id, sql):
    report = store.report(dataset_id)
    if not report:
        raise ValueError("Create a report first.")
    trace = Trace(store, dataset_id, "query", "offline", None)
    try:
        result = store.query(dataset_id, sql, [version["id"] for version in report["versions"]])
        trace.event("sql", {"sql": sql, "result": result})
        trace.finish(result)
        return dict(result, run_id=trace.id)
    except ValueError as exc:
        trace.finish(error=exc)
        raise
