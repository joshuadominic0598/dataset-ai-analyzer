"""Local HTTP transport, request boundaries, and endpoint dispatch."""

import json
import mimetypes
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..connections import ConnectionManager
from ..data import MAX_BYTES, MAX_CELL_CHARS, MAX_COLUMNS, MAX_ROWS, dump
from ..providers import connections, list_models
from .exports import export_html, export_markdown
from .services import (
    conversation_history,
    dataset_view,
    decode_upload,
    ensure_dataset_idle,
    import_demo,
    query_data,
    start_investigation,
)

STATIC = Path(__file__).resolve().parent.parent / "static"

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
            print(
                time.strftime("%H:%M:%S"),
                self.command,
                urlsplit(self.path).path,
                args[1] if len(args) > 1 else "",
            )

    def send(self, status, payload, content_type="application/json; charset=utf-8", filename=None):
        body = (
            dump(payload).encode()
            if content_type.startswith("application/json") and not isinstance(payload, bytes)
            else payload.encode()
            if isinstance(payload, str)
            else payload
        )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
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
        dataset_id = query.get("dataset", [None])[0]
        routes = {
            "/api/state": self._get_state,
            "/api/connections": self._get_connections,
            "/api/dataset": self._get_dataset,
            "/api/runs": self._get_runs,
            "/api/run": self._get_run,
            "/api/history": self._get_history,
            "/api/export": self._get_export,
            "/api/source": self._get_source,
        }
        route = routes.get(url.path)
        if route:
            return route(dataset_id, query)
        return self._get_static(url.path)

    def _get_state(self, dataset_id, query):
        return self.send(
            200,
            {
                "datasets": self.server.store.datasets(),
                "connections": connections(self.server.connections),
                "token": self.server.token,
                "watcher": self.server.watch_status,
                "limits": {
                    "bytes_per_file": MAX_BYTES,
                    "records_per_file": MAX_ROWS,
                    "columns_per_file": MAX_COLUMNS,
                    "characters_per_cell": MAX_CELL_CHARS,
                    "versions": 5,
                    "encoding": "UTF-8 CSV",
                },
            },
        )

    def _get_connections(self, dataset_id, query):
        return self.send(200, {"connections": connections(self.server.connections)})

    def _get_dataset(self, dataset_id, query):
        return self.send(200, dataset_view(self.server.store, dataset_id))

    def _get_runs(self, dataset_id, query):
        self.server.store.dataset(dataset_id)
        return self.send(200, {"runs": self.server.store.runs(dataset_id)})

    def _get_run(self, dataset_id, query):
        return self.send(200, self.server.store.run(query.get("id", [None])[0]))

    def _get_history(self, dataset_id, query):
        return self.send(200, conversation_history(self.server.store, dataset_id))

    def _get_export(self, dataset_id, query):
        report = self.server.store.report(dataset_id, query.get("report", [None])[0])
        if not report:
            raise ValueError("No report to export.")
        export_format = query.get("format", ["json"])[0]
        if export_format == "html":
            return self.send(
                200, export_html(report), "text/html; charset=utf-8", "analytic-brain-report.html"
            )
        if export_format == "md":
            return self.send(
                200,
                export_markdown(report),
                "text/markdown; charset=utf-8",
                "analytic-brain-report.md",
            )
        return self.send(200, report, filename="analytic-brain-report.json")

    def _get_source(self, dataset_id, query):
        content = self.server.store.source(dataset_id, query.get("version", [None])[0])
        return self.send(200, content, "text/csv; charset=utf-8", "snapshot.csv")

    def _get_static(self, path):
        files = {
            "/": "index.html",
            "/app.js": "app.js",
            "/styles.css": "styles.css",
            "/brain.svg": "brain.svg",
        }
        if path not in files:
            return self.send(404, {"error": "Page not found."})
        target = STATIC / files[path]
        content_type = (mimetypes.guess_type(str(target))[0] or "text/plain") + "; charset=utf-8"
        return self.send(200, target.read_bytes(), content_type)

    def do_POST(self):
        if not self.allowed_host() or not secrets.compare_digest(
            self.headers.get("X-Reviso-Token", ""), self.server.token
        ):
            return self.send(403, {"error": "Refresh the local page before making changes."})
        origin = self.headers.get("Origin")
        if origin and origin not in {
            f"http://localhost:{self.server.server_port}",
            f"http://127.0.0.1:{self.server.server_port}",
        }:
            return self.send(403, {"error": "Requests must come from the local page."})
        try:
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                return self.send(400, {"error": "Use a fixed-length JSON upload."})
            if (
                self.headers.get("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                self.close_connection = True
                return self.send(
                    415, {"error": "Send uploads as application/json with base64 CSV content."}
                )
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BYTES * 1.4 + 4096:
                self.close_connection = True
                return self.send(413, {"error": "The request exceeds the 20 MB file upload limit."})
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
            self.send(408, {"error": "The upload timed out. Try the file again."})

    def post(self, path, body):
        store = self.server.store
        if path == "/api/connections/save":
            return self.send(
                200,
                {
                    "connections": self.server.connections.save(
                        body.get("provider"), body.get("settings", {})
                    )
                },
            )
        if path == "/api/connections/models":
            provider = body.get("provider")
            return self.send(200, list_models(provider, self.server.connections.resolve(provider)))
        if path == "/api/datasets":
            return self.send(201, store.create_dataset(body.get("name", "")))
        dataset_id = body.get("dataset")
        dataset = store.dataset(dataset_id)
        if path in ("/api/upload", "/api/configure", "/api/demo"):
            ensure_dataset_idle(self.server, dataset_id)
        if path == "/api/upload":
            return self.send(
                201, store.ingest(dataset_id, body.get("label", ""), decode_upload(body))
            )
        if path == "/api/configure":
            return self.send(200, store.configure(dataset_id, body.get("config", {})))
        if path == "/api/demo":
            return self.send(201, import_demo(store, dataset_id))
        if path in ("/api/report", "/api/chat", "/api/index"):
            return self.send(202, start_investigation(self.server, dataset, path, body))
        if path == "/api/query":
            return self.send(200, query_data(store, dataset_id, body.get("sql")))
        if path == "/api/feedback":
            return self.send(
                200,
                store.save_feedback(
                    dataset_id, body.get("run_id"), body.get("rating"), body.get("note", "")
                ),
            )
        return self.send(404, {"error": "Action not found."})
