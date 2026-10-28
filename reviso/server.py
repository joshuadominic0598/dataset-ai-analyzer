"""Local application server. No dependency installation or cloud service is required."""

import argparse
import base64
import hashlib
import html
import json
import mimetypes
import os
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .agent import Trace, index_status, submit
from .analysis import discover
from .data import MAX_BYTES, MAX_ROWS, MAX_COLUMNS, MAX_CELL_CHARS, Store, dump, now
from .providers import connections, list_models
from .connections import ConnectionManager

STATIC = Path(__file__).parent / "static"


def export_markdown(report):
    lines = [f"# {report['dataset_name']}", "", f"Report: {report['id']} · Created: {report['created_at']}",
             f"Analytic Brain · Method: {report['method_version']}", "", "## Source files", ""]
    for v in report["versions"]:
        lines.append(f"- V{v['ordinal']} {v['label']}: {v['row_count']} records; SHA-256 {v['digest']}")
    if report["ai"]:
        lines += ["", "## AI interpretation", "", report["ai"]["summary"]]
        for insight in report["ai"]["insights"]:
            lines += ["", insight["interpretation"] + " [" + ", ".join(insight["evidence_ids"]) + "]", "Next: " + insight["next_step"]]
        if report["ai"].get("query_results"):
            lines += ["", "## Supporting queries", "", "```json", json.dumps(report["ai"]["query_results"], indent=2, ensure_ascii=False), "```"]
    lines += ["", "## Findings", ""]
    for f in report["findings"]:
        scope = f"V{f['to']} · dataset analysis" if f.get("scope") == "snapshot" else f"V{f['from']} → V{f['to']}"
        lines += [f"### [{f['id']}] {f['title']}", f"{scope} · {f['severity']} · {f['assessment']}", "", f["detail"], "", "Next: " + f["action"], "", "```json", json.dumps({"metrics":f["metrics"],"examples":f.get("examples",[])}, indent=2, ensure_ascii=False), "```", ""]
    if report.get("exploration"):
        lines += ["## Latest file exploration", "", "```json", json.dumps(report["exploration"], indent=2, ensure_ascii=False), "```", ""]
    lines += ["## Limits", ""] + ["- " + line for line in report["limitations"]]
    return "\n".join(lines)


def export_html(report):
    e = html.escape
    findings = "".join(f"<article><small>{e(f['id'])} · {('V'+str(f['to'])+' · dataset analysis') if f.get('scope')=='snapshot' else ('V'+str(f['from'])+' → V'+str(f['to']))} · {e(f['severity'])}</small><h2>{e(f['title'])}</h2><p>{e(f['detail'])}</p><p><b>Next step</b> {e(f['action'])}</p><details><summary>Evidence and record examples</summary><pre>{e(json.dumps({'metrics':f['metrics'],'examples':f.get('examples',[])}, indent=2, ensure_ascii=False))}</pre></details></article>" for f in report["findings"])
    if report.get("exploration"):
        findings += "<article><h2>Latest file exploration</h2><details><summary>Full field profiles and exploration results</summary><pre>" + e(json.dumps(report["exploration"],indent=2,ensure_ascii=False)) + "</pre></details></article>"
    versions = "".join(f"<li>V{v['ordinal']} · {e(v['label'])} · {v['row_count']:,} records<br><code>{e(v['digest'])}</code></li>" for v in report["versions"])
    interpretation = ""
    if report["ai"]:
        interpretation = "<article><small>AI interpretation</small><p>" + e(report["ai"]["summary"]) + "</p>" + "".join("<p>" + e(i["interpretation"]) + " [" + e(", ".join(i["evidence_ids"])) + "]</p>" for i in report["ai"]["insights"]) + "</article>"
        if report["ai"].get("query_results"):
            interpretation += "<article><h2>Supporting queries</h2><pre>" + e(json.dumps(report["ai"]["query_results"], indent=2, ensure_ascii=False)) + "</pre></article>"
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{e(report['dataset_name'])} — Analytic Brain</title>
<style>:root{{color-scheme:dark}}body{{font:16px/1.65 system-ui;background:#0b0b0d;color:#f4f1ed;max-width:920px;margin:60px auto;padding:24px}}h1{{font-size:46px;line-height:1.1}}small{{color:#ffc397}}article{{background:#161618;border:1px solid #353237;border-radius:16px;padding:26px;margin:18px 0;break-inside:avoid}}summary{{color:#ff8a3d;cursor:pointer}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;color:#cdc7c0}}code{{font-size:11px;overflow-wrap:anywhere;color:#ffc397}}li{{margin:14px 0}}@media print{{body{{margin:0}}details{{display:block}}}}</style>
<small>ANALYTIC BRAIN / DATASET REPORT</small><h1>{e(report['dataset_name'])}</h1><p>{report['summary']['versions']} file(s) · {report['summary']['latest_records']:,} latest records · {report['summary']['attention']} findings needing attention</p><p>Created {e(report['created_at'])}<br>Report {e(report['id'])} · {e(report['method_version'])}</p>{interpretation}{findings}<h2>Source snapshots</h2><ul>{versions}</ul><h2>Limits</h2><ul>{''.join('<li>'+e(v)+'</li>' for v in report['limitations'])}</ul></html>"""


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store):
        super().__init__(address, Handler)
        self.store = store
        self.token = secrets.token_urlsafe(32)
        self.watch_status = None
        self.jobs_lock = threading.Lock()
        self.active_jobs = {}
        self.connections = ConnectionManager(store.path.parent / "connections.json")


class Handler(BaseHTTPRequestHandler):
    server_version = "AnalyticBrain/0.2"

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt, *args):
        # Avoid logging questions, upload names, SQL text, or query-string contents.
        if args and isinstance(args[0], str):
            print(time.strftime("%H:%M:%S"), self.command, urlsplit(self.path).path, args[1] if len(args) > 1 else "")

    def send(self, status, payload, content_type="application/json; charset=utf-8", filename=None):
        body = dump(payload).encode() if content_type.startswith("application/json") and not isinstance(payload, bytes) else payload.encode() if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host", "") in {f"localhost:{port}", f"127.0.0.1:{port}"}

    def do_GET(self):
        if not self.allowed_host():
            return self.send(403, {"error": "Use the local server address."})
        try:
            self.get()
        except (ValueError, KeyError, TypeError) as exc:
            self.send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def get(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        path, store = url.path, self.server.store
        dataset_id = query.get("dataset", [None])[0]
        if path == "/api/state":
            return self.send(200, {"datasets": store.datasets(), "connections": connections(self.server.connections), "token": self.server.token, "watcher": self.server.watch_status,
                                   "limits": {"bytes_per_file":MAX_BYTES,"records_per_file":MAX_ROWS,"columns_per_file":MAX_COLUMNS,"characters_per_cell":MAX_CELL_CHARS,"versions":5,"encoding":"UTF-8 CSV"}})
        if path == "/api/connections":
            return self.send(200, {"connections": connections(self.server.connections)})
        if path == "/api/dataset":
            dataset = store.dataset(dataset_id)
            versions = store.versions(dataset_id)
            report = store.report(dataset_id)
            stale = bool(report and ([v["digest"] for v in versions] != [v["digest"] for v in report["versions"]] or dataset["config"] != report["config"]))
            return self.send(200, {"dataset": dataset, "versions": versions, "discovery": discover(store, dataset_id), "report": report,
                                   "report_stale": stale, "index": index_status(store, report) if report else None})
        if path == "/api/runs":
            store.dataset(dataset_id)
            return self.send(200, {"runs": store.runs(dataset_id)})
        if path == "/api/run":
            return self.send(200, store.run(query.get("id", [None])[0]))
        if path == "/api/history":
            report = store.report(dataset_id)
            messages = store.history(dataset_id, report["id"]) if report else []
            for message in messages:
                if message["role"] == "assistant":
                    result = store.run(message["run_id"])["payload"].get("result") or {}
                    message["evidence"] = result.get("evidence")
            return self.send(200, {"messages": messages})
        if path == "/api/export":
            report = store.report(dataset_id, query.get("report", [None])[0])
            if not report:
                raise ValueError("No report to export.")
            fmt = query.get("format", ["json"])[0]
            if fmt == "html":
                return self.send(200, export_html(report), "text/html; charset=utf-8", "analytic-brain-report.html")
            if fmt == "md":
                return self.send(200, export_markdown(report), "text/markdown; charset=utf-8", "analytic-brain-report.md")
            return self.send(200, report, filename="analytic-brain-report.json")
        if path == "/api/source":
            content = store.source(dataset_id, query.get("version", [None])[0])
            return self.send(200, content, "text/csv; charset=utf-8", "snapshot.csv")
        files = {"/": "index.html", "/app.js": "app.js", "/styles.css": "styles.css", "/brain.svg":"brain.svg"}
        if path not in files:
            return self.send(404, {"error": "Page not found."})
        target = STATIC / files[path]
        return self.send(200, target.read_bytes(), (mimetypes.guess_type(str(target))[0] or "text/plain") + "; charset=utf-8")

    def do_POST(self):
        if not self.allowed_host() or not secrets.compare_digest(self.headers.get("X-Reviso-Token", ""), self.server.token):
            return self.send(403, {"error": "Refresh the local page before making changes."})
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}:
            return self.send(403, {"error": "Requests must come from the local page."})
        try:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self.send(400, {"error":"Use a fixed-length JSON upload."})
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                self.close_connection = True
                return self.send(415, {"error":"Send uploads as application/json with base64 CSV content."})
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BYTES * 1.4 + 4096:
                self.close_connection = True
                return self.send(413, {"error":"The request exceeds the 20 MB file upload limit."})
            if length < 2:
                raise ValueError("The request is empty or has an invalid content length.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            self.post(urlsplit(self.path).path, body)
        except (ValueError, TypeError, KeyError) as exc:
            self.send(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, socket.timeout):
            self.close_connection = True
            self.send(408, {"error":"The upload timed out. Try the file again."})

    def post(self, path, body):
        store = self.server.store
        if path == "/api/connections/save":
            return self.send(200, {"connections": self.server.connections.save(body.get("provider"), body.get("settings", {}))})
        if path == "/api/connections/models":
            provider = body.get("provider")
            return self.send(200, list_models(provider, self.server.connections.resolve(provider)))
        dataset_id = body.get("dataset")
        if path == "/api/datasets":
            return self.send(201, store.create_dataset(body.get("name", "")))
        dataset = store.dataset(dataset_id)
        if path in ("/api/upload", "/api/configure", "/api/demo"):
            with self.server.jobs_lock:
                active = self.server.active_jobs.get(dataset_id)
                if active and store.run(active)["status"] == "running":
                    raise ValueError("Wait for the active investigation before changing its data or setup.")
        if path == "/api/upload":
            filename = body.get("filename")
            if filename is not None and (not isinstance(filename,str) or not filename.lower().endswith(".csv") or any(c in filename for c in ("/","\\","\x00"))):
                raise ValueError("Choose a CSV file with a plain filename.")
            if body.get("content_type", "") not in ("", "text/csv", "application/csv", "text/plain", "application/vnd.ms-excel", "application/octet-stream"):
                raise ValueError("Choose a CSV text file. The file content must also pass CSV validation.")
            try:
                content = base64.b64decode(body.get("content", ""), validate=True)
            except (ValueError, TypeError):
                raise ValueError("The uploaded file could not be decoded.") from None
            return self.send(201, store.ingest(dataset_id, body.get("label", ""), content))
        if path == "/api/configure":
            return self.send(200, store.configure(dataset_id, body.get("config", {})))
        if path == "/api/demo":
            if store.versions(dataset_id):
                raise ValueError("Load the demo into a new, empty dataset timeline.")
            from .demo import load_demo
            load_demo(store, dataset_id)
            return self.send(201, {"loaded": True})
        if path in ("/api/report", "/api/chat", "/api/index"):
            provider, model = body.get("provider", "offline"), body.get("model")
            if provider not in ("offline", "ollama", "openai", "gemini", "groq"):
                raise ValueError("Choose a supported provider.")
            if provider != "offline":
                model = model or self.server.connections.resolve(provider)["model"]
            if provider not in ("offline", "ollama") and not dataset["config"]["cloud_allowed"]:
                raise ValueError("Allow cloud analysis in setup before using a provider.")
            kind = {"/api/report": "report", "/api/chat": "chat", "/api/index": "index"}[path]
            # One job per dataset pins imports/configuration for report generation and chat.
            with self.server.jobs_lock:
                previous = self.server.active_jobs.get(dataset_id)
                if previous and store.run(previous)["status"] == "running":
                    raise ValueError("This dataset already has a running investigation. Wait for it to finish.")
                identifier = submit(store, dataset_id, kind, provider, model, body.get("question", ""), self.server.connections.client_factory(), body.get("investigation"))
                self.server.active_jobs[dataset_id] = identifier
            return self.send(202, {"run_id": identifier})
        if path == "/api/query":
            report = store.report(dataset_id)
            if not report:
                raise ValueError("Create a report first.")
            trace = Trace(store, dataset_id, "query", "offline", None)
            try:
                result = store.query(dataset_id, body.get("sql"), [v["id"] for v in report["versions"]])
                trace.event("sql", {"sql": body.get("sql"), "result": result})
                trace.finish(result)
                return self.send(200, dict(result, run_id=trace.id))
            except ValueError as exc:
                trace.finish(error=exc)
                raise
        if path == "/api/feedback":
            run = store.run(body.get("run_id"))
            rating = body.get("rating")
            if run["dataset_id"] != dataset_id or rating not in (-1, 1):
                raise ValueError("Choose useful or needs work for a run in this dataset.")
            with store.db() as db:
                db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?)", (run["id"], rating, str(body.get("note", ""))[:500], now()))
            return self.send(200, {"saved": True})
        self.send(404, {"error": "Action not found."})


def watch_file(server, source, dataset_id, interval, stop_event=None):
    """Capture stable full-file snapshots, including before a later overwrite."""
    source = Path(source).expanduser().resolve()
    server.store.dataset(dataset_id)
    versions = server.store.versions(dataset_id)
    last_digest = versions[-1]["digest"] if versions else None
    previous_stat = None
    server.watch_status = {"file": source.name, "dataset": dataset_id, "status": "watching", "message": "Waiting for a stable file."}
    while True:
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)
        try:
            if len(server.store.versions(dataset_id)) >= 5:
                server.watch_status.update(status="capacity_reached", message="Five versions are preserved. Start another timeline to capture more.")
                return
            stat = source.stat()
            signature = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
            if signature != previous_stat:
                previous_stat = signature
                continue
            if stat.st_size > MAX_BYTES:
                raise ValueError("The watched file exceeds 20 MB.")
            content = source.read_bytes()
            if source.stat().st_mtime_ns != stat.st_mtime_ns:
                previous_stat = None
                continue
            digest = hashlib.sha256(content).hexdigest()
            if digest == last_digest:
                continue
            with server.jobs_lock:
                active = server.active_jobs.get(dataset_id)
                if active and server.store.run(active)["status"] == "running":
                    continue
                server.store.ingest(dataset_id, "Captured " + now()[:19], content)
            last_digest = digest
            server.watch_status.update(status="watching", message="Saved a new snapshot. Generate a report to review it.")
        except (ValueError, OSError) as exc:
            server.watch_status.update(status="needs_attention", message=str(exc))


def main():
    parser = argparse.ArgumentParser(description="Analytic Brain — understand your data.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default=os.environ.get("REVISO_DB", ".reviso/reviso.sqlite"))
    parser.add_argument("--demo", action="store_true", help="Create a five-version house-price demo if no dataset exists.")
    parser.add_argument("--watch", help="Watch a local CSV for stable changes and preserve full snapshots.")
    parser.add_argument("--dataset", help="Dataset ID receiving watched snapshots.")
    parser.add_argument("--interval", type=float, default=5)
    args = parser.parse_args()
    store = Store(args.db)
    store.recover_interrupted_runs()
    if args.demo and not store.datasets():
        from .demo import load_demo
        dataset = store.create_dataset("Ames housing")
        load_demo(store, dataset["id"])
        trace = Trace(store, dataset["id"], "report", "offline", None)
        from .agent import generate_report
        trace.finish(generate_report(store, dataset["id"], trace))
    server = AppServer(("127.0.0.1", args.port), store)
    if args.watch:
        if not args.dataset or args.interval < 1:
            parser.error("--watch requires --dataset and an --interval of at least one second.")
        store.dataset(args.dataset)
        threading.Thread(target=watch_file, args=(server, args.watch, args.dataset, args.interval), daemon=True).start()
    print(f"Analytic Brain is running at http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
