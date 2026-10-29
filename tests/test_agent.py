import tempfile
import unittest
from pathlib import Path

from reviso.agent import PROMPT_HASH, Trace, build_index, chat, generate_report, semantic_search
from reviso.data import Store
from reviso.providers import ProviderError
from support.fakes import FakeClient

class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "test.sqlite")
        self.id = self.store.create_dataset("Test")["id"]
        self.store.ingest(
            self.id,
            "Before",
            b"id,value,notes\n1,10,A quiet garden house with a green lawn and lots of room for children to play.\n2,20,A city apartment close to the train station with access to local shops.\n",
        )
        self.store.ingest(
            self.id,
            "After",
            b"id,value,notes\n1,10,A quiet garden house with a green lawn and lots of room for children to play.\n2,30,A city apartment close to the train station with access to local shops.\n",
        )
        self.store.configure(
            self.id, {"identity": ["id"], "setup_complete": True, "cloud_allowed": True}
        )
        FakeClient.answers, FakeClient.calls = [], []

    def trace(self, kind="report"):
        return Trace(self.store, self.id, kind, "gemini", "test-model")

    def local_report(self):
        trace = self.trace()
        result = generate_report(self.store, self.id, trace)
        trace.finish(result)
        return self.store.report(self.id)

    def test_local_report_has_no_model_calls(self):
        report = self.local_report()
        self.assertIsNone(report["ai"])
        run = self.store.runs(self.id)[0]
        self.assertEqual(run["payload"]["prompt_hash"], PROMPT_HASH)
        self.assertFalse(any(e["kind"] == "model_call" for e in run["payload"]["events"]))

    def test_report_agent_executes_plan_then_cites_evidence(self):
        FakeClient.answers = [
            lambda c: {
                "plan": ["Inspect the latest values."],
                "queries": [
                    {
                        "purpose": "Inspect prices",
                        "sql": f'SELECT value FROM "{c["schema"][-1]["table"]}" LIMIT 10',
                    }
                ],
            },
            lambda c: {
                "summary": "One record was edited.",
                "insights": [
                    {
                        "evidence_ids": [c["findings"][0]["id"]],
                        "interpretation": "A record has a different value.",
                        "next_step": "Confirm the edit was expected.",
                    }
                ],
            },
        ]
        trace = self.trace()
        generate_report(self.store, self.id, trace, "gemini", "test", FakeClient)
        report = self.store.report(self.id)
        self.assertEqual(report["ai"]["query_results"][0]["result"]["rows"], [[10], [30]])
        self.assertTrue(any(e["kind"] == "sql" for e in trace.payload["events"]))

    def test_invalid_citations_are_not_published(self):
        FakeClient.answers = [
            {"plan": [], "queries": []},
            *(
                [
                    {
                        "summary": "Changed.",
                        "insights": [
                            {
                                "evidence_ids": ["F999"],
                                "interpretation": "Changed.",
                                "next_step": "Check it.",
                            }
                        ],
                    }
                ]
                * 2
            ),
        ]
        with self.assertRaisesRegex(ProviderError, "unavailable evidence"):
            generate_report(self.store, self.id, self.trace(), "gemini", "test", FakeClient)
        self.assertIsNone(self.store.report(self.id))

    def test_unsupported_numeric_claim_is_not_published(self):
        FakeClient.answers = [
            {"plan": [], "queries": []},
            *([{"summary": "There were 999999999 lost records.", "insights": []}] * 2),
        ]
        with self.assertRaisesRegex(ProviderError, "number absent"):
            generate_report(self.store, self.id, self.trace(), "gemini", "test", FakeClient)

    def test_cloud_consent_required_before_model_call(self):
        self.store.configure(self.id, {"cloud_allowed": False})
        with self.assertRaisesRegex(ValueError, "Allow cloud"):
            generate_report(self.store, self.id, self.trace(), "gemini", "test", FakeClient)
        self.assertEqual(FakeClient.calls, [])

    def test_ollama_report_and_chat_do_not_require_cloud_permission(self):
        self.store.configure(self.id, {"cloud_allowed": False})
        FakeClient.answers = [
            {"plan": [], "queries": []},
            lambda c: {
                "summary": "A value changed.",
                "insights": [
                    {
                        "evidence_ids": [c["findings"][0]["id"]],
                        "interpretation": "An existing record was edited.",
                        "next_step": "Check the source.",
                    }
                ],
            },
        ]
        generate_report(self.store, self.id, self.trace(), "ollama", "qwen3:8b", FakeClient)
        FakeClient.answers = [{"answer": "Which field should I inspect?", "evidence_ids": []}]
        result = chat(
            self.store,
            self.id,
            "Help me investigate",
            self.trace("chat"),
            "ollama",
            "qwen3:8b",
            FakeClient,
        )
        self.assertEqual(result["provider"], "ollama")

    def test_ollama_text_index_does_not_require_cloud_permission(self):
        self.store.configure(
            self.id,
            {
                "cloud_allowed": False,
                "semantic": ["notes"],
                "embedding_provider": "ollama",
                "embedding_model": "local-test-embedding",
            },
        )
        report = self.local_report()
        result = build_index(self.store, self.id, self.trace("index"), FakeClient)
        self.assertTrue(result["ready"])
        matches = semantic_search(self.store, report, "garden", 2, self.trace("chat"), FakeClient)
        self.assertEqual(matches["matches"][0]["row"], 1)

    def test_local_chat_is_labeled_and_sql_backed(self):
        report = self.local_report()
        result = chat(
            self.store,
            self.id,
            'SELECT count(*) FROM "' + report["versions"][-1]["table"] + '"',
            self.trace("chat"),
        )
        self.assertEqual(result["evidence"]["Q1"]["rows"], [[2]])
        self.assertEqual(result["mode"], "local")

    def test_chat_tool_loop_uses_actual_sql_results(self):
        self.local_report()
        FakeClient.answers = [
            lambda c: {
                "tool": "sql",
                "sql": f'SELECT max(value) FROM "{c["schema"][-1]["table"]}" LIMIT 1',
                "purpose": "Check highest value",
            },
            {"answer": "The highest value is 30. [Q1]", "evidence_ids": ["Q1"]},
        ]
        result = chat(
            self.store,
            self.id,
            "What is the highest value?",
            self.trace("chat"),
            "gemini",
            "test",
            FakeClient,
        )
        self.assertIn("30", result["answer"])
        self.assertEqual(result["evidence"]["Q1"]["result"]["rows"], [[30]])
        self.assertEqual(len(FakeClient.calls), 2)

    def test_chat_unknown_tool_and_mutation_cannot_execute(self):
        self.local_report()
        FakeClient.answers = [
            {"tool": "sql", "sql": "DROP TABLE datasets"},
            lambda c: {
                "answer": "I could not run that operation. Can you ask a question about the saved data?",
                "evidence_ids": [],
            },
        ]
        result = chat(
            self.store,
            self.id,
            "Please drop the data",
            self.trace("chat"),
            "gemini",
            "test",
            FakeClient,
        )
        self.assertEqual(self.store.dataset(self.id)["id"], self.id)
        self.assertEqual(result["evidence_ids"], [])

    def test_chat_has_finite_tool_budget(self):
        self.local_report()
        FakeClient.answers = [{"tool": "sql", "sql": "SELECT 1"} for _ in range(7)]
        with self.assertRaisesRegex(ProviderError, "tool limit"):
            chat(
                self.store,
                self.id,
                "Keep looking",
                self.trace("chat"),
                "gemini",
                "test",
                FakeClient,
            )

    def test_chat_remains_pinned_to_saved_report(self):
        report = self.local_report()
        extra = self.store.ingest(self.id, "Later", b"id,value,notes\n3,999,later\n")
        FakeClient.answers = [
            {"tool": "sql", "sql": f'SELECT * FROM "{extra["table"]}"'},
            {
                "answer": "That later snapshot is outside this saved report. Should a new report include it?",
                "evidence_ids": [],
            },
        ]
        result = chat(
            self.store,
            self.id,
            "Read future data",
            self.trace("chat"),
            "gemini",
            "test",
            FakeClient,
        )
        self.assertEqual(result["report_id"], report["id"])
        self.assertIn("error", FakeClient.calls[-1]["tool_results"][0]["result"])

    def test_consent_revocation_blocks_old_report_cloud_chat(self):
        self.local_report()
        self.store.configure(self.id, {"cloud_allowed": False})
        with self.assertRaisesRegex(ValueError, "Allow cloud"):
            chat(
                self.store,
                self.id,
                "What changed?",
                self.trace("chat"),
                "gemini",
                "test",
                FakeClient,
            )

    def test_semantic_search_needs_consent_and_selection(self):
        self.local_report()
        with self.assertRaisesRegex(ValueError, "Select text"):
            build_index(self.store, self.id, self.trace("index"), FakeClient)

    def test_semantic_index_is_versioned_and_reused(self):
        self.store.configure(
            self.id,
            {
                "semantic": ["notes"],
                "embedding_provider": "gemini",
                "embedding_model": "test-embedding",
            },
        )
        report = self.local_report()
        trace = self.trace("index")
        result = build_index(self.store, self.id, trace, FakeClient)
        self.assertTrue(result["ready"])
        self.assertEqual(result["indexed"], 4)
        calls_before = len([e for e in trace.payload["events"] if e["kind"] == "model_call"])
        build_index(self.store, self.id, trace, FakeClient)
        self.assertEqual(
            len([e for e in trace.payload["events"] if e["kind"] == "model_call"]), calls_before
        )
        result = semantic_search(self.store, report, "garden", 2, trace, FakeClient)
        self.assertEqual(result["matches"][0]["row"], 1)
        self.assertEqual(result["matches"][0]["version"], 2)

    def test_history_is_scoped_to_report_and_errors_do_not_save_answers(self):
        self.local_report()
        result = chat(self.store, self.id, "What changed?", self.trace("chat"))
        self.assertEqual(len(self.store.history(self.id, result["report_id"])), 2)
        next_report = self.local_report()
        self.assertEqual(self.store.history(self.id, next_report["id"]), [])

if __name__ == "__main__":
    unittest.main()
