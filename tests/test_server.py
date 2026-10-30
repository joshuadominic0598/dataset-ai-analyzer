import base64
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from reviso.data import Store
from reviso.server import AppServer, export_html, export_markdown

class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "test.sqlite")
        try:
            cls.server = AppServer(("127.0.0.1", 0), cls.store)
        except PermissionError:
            cls.temp.cleanup()
            raise unittest.SkipTest(
                "Loopback sockets are restricted in this sandbox. Run this suite with local network permission."
            )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, path, body=None, token=True, extra=None):
        headers = extra or {}
        if token:
            headers["X-Reviso-Token"] = self.server.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                content = response.read()
                return (
                    response.status,
                    response.headers,
                    json.loads(content)
                    if response.headers["Content-Type"].startswith("application/json")
                    else content,
                )
        except urllib.error.HTTPError as error:
            return error.code, error.headers, json.loads(error.read())

    def dataset(self):
        return self.request("/api/datasets", {"name": "API test"})[2]["id"]

    def upload(self, dataset, label, content):
        return self.request(
            "/api/upload",
            {"dataset": dataset, "label": label, "content": base64.b64encode(content).decode()},
        )

    def wait(self, identifier):
        for _ in range(50):
            run = self.request("/api/run?id=" + identifier)[2]
            if run["status"] != "running":
                return run
            time.sleep(0.05)
        self.fail("Run did not complete.")

    def test_static_page_and_headers(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Analytic Brain", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_single_file_chat_before_report_then_full_exploration(self):
        dataset = self.dataset()
        self.assertEqual(self.upload(dataset, "Only file", b"id,price\n1,10\n2,\n3,30\n")[0], 201)
        status, _, created = self.request(
            "/api/chat", {"dataset": dataset, "question": "What is missing?", "provider": "offline"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(created["run_id"])["status"], "completed")
        draft = self.request("/api/dataset?dataset=" + dataset)[2]["report"]
        self.assertTrue(draft["draft"])
        self.assertEqual(len(self.request("/api/history?dataset=" + dataset)[2]["messages"]), 2)
        self.assertEqual(
            self.request(
                "/api/configure",
                {
                    "dataset": dataset,
                    "config": {
                        "setup_complete": True,
                        "dataset_type": "prices",
                        "target_column": "price",
                    },
                },
            )[0],
            200,
        )
        created = self.request("/api/report", {"dataset": dataset, "provider": "offline"})[2]
        self.assertEqual(self.wait(created["run_id"])["status"], "completed")
        final = self.request("/api/dataset?dataset=" + dataset)[2]["report"]
        self.assertFalse(final["draft"])
        self.assertEqual(final["mode"], "exploration")
        self.assertEqual(final["exploration"]["fields"]["price"]["missing"], 1)
        self.assertEqual(final["comparisons"], [])
        self.assertIn("dataset analysis", export_markdown(final))
        self.assertIn("Analytic Brain", export_html(final))

    def test_report_runs_immediately_without_setup_answers(self):
        dataset = self.dataset()
        self.upload(dataset, "Only file", b"id,price\n1,10\n2,20\n")
        status, _, created = self.request(
            "/api/report", {"dataset": dataset, "provider": "offline"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(created["run_id"])["status"], "completed")
        view = self.request("/api/dataset?dataset=" + dataset)[2]
        self.assertFalse(view["report"]["draft"])
        self.assertFalse(view["report_stale"])
        self.assertEqual(view["report"]["analysis_config"]["target_column"], "price")
        self.assertEqual(view["report"]["config"]["analysis_goal"], "")

    def test_upload_metadata_and_size_protocol(self):
        limits = self.request("/api/state")[2]["limits"]
        self.assertEqual(limits["versions"], 5)
        self.assertEqual(limits["columns_per_file"], 200)
        dataset = self.dataset()
        base = {
            "dataset": dataset,
            "label": "Test",
            "content": base64.b64encode(b"x\n1\n").decode(),
        }
        for metadata in (
            {"filename": "payload.exe"},
            {"filename": "../source.csv"},
            {"content_type": "application/pdf"},
        ):
            with self.subTest(metadata=metadata):
                self.assertEqual(self.request("/api/upload", {**base, **metadata})[0], 400)
        self.assertEqual(
            self.request(
                "/api/upload", {**base, "filename": "data.csv", "content_type": "text/csv"}
            )[0],
            201,
        )
        self.assertEqual(
            self.request(
                "/api/datasets", {"name": "Denied"}, extra={"Transfer-Encoding": "chunked"}
            )[0],
            400,
        )
        request = urllib.request.Request(
            self.base + "/api/upload",
            data=b"{}",
            headers={"X-Reviso-Token": self.server.token, "Content-Type": "text/plain"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 415)
        with patch("reviso.server.http.MAX_BYTES", 1024):
            self.assertEqual(self.request("/api/upload", {**base, "content": "x" * 6000})[0], 413)

    def test_invalid_effort_is_rejected_before_starting_a_run(self):
        dataset = self.dataset()
        self.upload(dataset, "One", b"x\n1\n")
        before = len(self.store.runs(dataset))
        status, _, error = self.request(
            "/api/chat",
            {
                "dataset": dataset,
                "provider": "offline",
                "question": "Explore",
                "investigation": {"effort": "high", "context_window": 8192},
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("32,768", error["error"])
        self.assertEqual(len(self.store.runs(dataset)), before)

    def test_csrf_and_cross_origin_requests_rejected(self):
        self.assertEqual(self.request("/api/datasets", {"name": "Denied"}, token=False)[0], 403)
        self.assertEqual(
            self.request(
                "/api/datasets", {"name": "Denied"}, extra={"Origin": "https://outside.example"}
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "/api/connections/save", {"provider": "ollama", "settings": {}}, token=False
            )[0],
            403,
        )

    def test_connections_can_be_set_up_before_creating_a_dataset(self):
        status, _, body = self.request("/api/connections")
        self.assertEqual(status, 200)
        self.assertEqual(
            {c["id"] for c in body["connections"]}, {"ollama", "openai", "gemini", "groq"}
        )
        status, _, body = self.request(
            "/api/connections/save",
            {
                "provider": "gemini",
                "settings": {
                    "api_key": "api-test-private-key",
                    "model": "gemini-test",
                    "tier": "free",
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertNotIn("api-test-private-key", json.dumps(body))
        with patch(
            "reviso.server.http.list_models",
            return_value={"provider": "gemini", "models": [{"id": "gemini-test"}]},
        ) as models:
            status, _, body = self.request("/api/connections/models", {"provider": "gemini"})
            self.assertEqual(status, 200)
            self.assertEqual(models.call_args.args[1]["api_key"], "api-test-private-key")
            self.assertNotIn("api-test-private-key", json.dumps(body))
        self.assertEqual(
            self.request(
                "/api/connections/save",
                {"provider": "ollama", "settings": {"base_url": "http://remote.example"}},
            )[0],
            400,
        )

    def test_ollama_report_job_uses_default_without_cloud_consent(self):
        from support.fakes import FakeClient

        dataset = self.dataset()
        self.upload(dataset, "Before", b"id,value\n1,10\n")
        self.upload(dataset, "After", b"id,value\n1,20\n")
        self.request(
            "/api/configure",
            {
                "dataset": dataset,
                "config": {"identity": ["id"], "setup_complete": True, "cloud_allowed": False},
            },
        )
        FakeClient.answers = [
            {"plan": [], "queries": []},
            lambda c: {
                "summary": "A value changed.",
                "insights": [
                    {
                        "evidence_ids": [c["findings"][0]["id"]],
                        "interpretation": "A record was edited.",
                        "next_step": "Check the source.",
                    }
                ],
            },
        ]
        with patch.object(self.server.connections, "client_factory", return_value=FakeClient):
            status, _, created = self.request(
                "/api/report", {"dataset": dataset, "provider": "ollama"}
            )
            self.assertEqual(status, 202)
            run = self.wait(created["run_id"])
        self.assertEqual(run["status"], "completed", run["payload"])
        self.assertEqual(run["payload"]["model"], "qwen3:8b")
        self.assertEqual(
            self.request("/api/dataset?dataset=" + dataset)[2]["report"]["ai"]["provider"], "ollama"
        )

    def test_host_rebinding_rejected(self):
        self.assertEqual(self.request("/api/state", extra={"Host": "outside.example"})[0], 403)

    def test_upload_setup_report_chat_and_exports(self):
        dataset = self.dataset()
        self.assertEqual(self.upload(dataset, "Before", b"id,price\n1,10\n2,20\n")[0], 201)
        self.assertEqual(self.upload(dataset, "After", b"id,price\n1,11\n2,20\n3,30\n")[0], 201)
        self.assertEqual(
            self.request(
                "/api/configure",
                {"dataset": dataset, "config": {"identity": ["id"], "setup_complete": True}},
            )[0],
            200,
        )
        status, _, created = self.request(
            "/api/report", {"dataset": dataset, "provider": "offline"}
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(created["run_id"])["status"], "completed")
        data = self.request("/api/dataset?dataset=" + dataset)[2]
        self.assertEqual(data["report"]["comparisons"][0]["movement"]["added"], 1)
        self.assertFalse(data["report_stale"])
        _, _, created = self.request(
            "/api/chat", {"dataset": dataset, "question": "What changed?", "provider": "offline"}
        )
        self.assertEqual(self.wait(created["run_id"])["status"], "completed")
        self.assertEqual(len(self.request("/api/history?dataset=" + dataset)[2]["messages"]), 2)
        for fmt in ("html", "md", "json"):
            with self.subTest(fmt=fmt):
                status, headers, body = self.request(
                    "/api/export?dataset=" + dataset + "&format=" + fmt
                )
                self.assertEqual(status, 200)
                self.assertIn("attachment", headers["Content-Disposition"])
        self.upload(dataset, "Later", b"id,price\n1,11\n2,20\n3,30\n4,40\n")
        self.assertTrue(self.request("/api/dataset?dataset=" + dataset)[2]["report_stale"])

    def test_failed_query_has_inspectable_run(self):
        dataset = self.dataset()
        for label in ("Before", "After"):
            self.upload(dataset, label, b"id\n1\n")
        self.request("/api/configure", {"dataset": dataset, "config": {"setup_complete": True}})
        created = self.request("/api/report", {"dataset": dataset})[2]
        self.wait(created["run_id"])
        status, _, body = self.request(
            "/api/query", {"dataset": dataset, "sql": "DROP TABLE datasets"}
        )
        self.assertEqual(status, 400)
        runs = self.request("/api/runs?dataset=" + dataset)[2]["runs"]
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIn("SELECT", runs[0]["payload"]["error"])

    def test_sources_scoped_to_dataset(self):
        dataset = self.dataset()
        version = self.upload(dataset, "Original", b"x\nprivate\n")[2]
        other = self.dataset()
        self.assertEqual(
            self.request("/api/source?dataset=" + other + "&version=" + version["id"])[0], 400
        )

    def test_feedback_is_saved_against_actual_run(self):
        dataset = self.dataset()
        from reviso.agent import Trace

        trace = Trace(self.store, dataset, "test", "offline", None)
        trace.finish({"ok": True})
        self.assertEqual(
            self.request("/api/feedback", {"dataset": dataset, "run_id": trace.id, "rating": 1})[0],
            200,
        )
        other = self.dataset()
        self.assertEqual(
            self.request("/api/feedback", {"dataset": other, "run_id": trace.id, "rating": 1})[0],
            400,
        )

    def test_exports_escape_dataset_and_cell_content(self):
        from reviso.analysis import build_report

        dataset = self.store.create_dataset("<script>alert(1)</script>")["id"]
        for label in ("Before", "After"):
            self.store.ingest(dataset, label, b"id,price\n1,10\n")
        self.store.configure(dataset, {"setup_complete": True})
        report = build_report(self.store, dataset)
        rendered = export_html(report)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        report["ai"] = {
            "summary": "Observed changes.",
            "insights": [],
            "query_results": [{"id": "Q1", "result": {"value": "<script>bad</script>"}}],
        }
        self.assertIn("Supporting queries", export_html(report))
        self.assertNotIn("<script>", export_html(report))
        self.assertIn("Q1", export_markdown(report))

    def test_orphaned_runs_recover_as_failed(self):
        from reviso.agent import Trace

        dataset = self.dataset()
        trace = Trace(self.store, dataset, "test", "offline", None)
        self.store.recover_interrupted_runs()
        self.assertEqual(self.store.run(trace.id)["status"], "failed")

if __name__ == "__main__":
    unittest.main()
