import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from reviso.providers import Client, ProviderError, _read_json, connections
from reviso.rate_limits import RateLimiter

class Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, *args):
        return json.dumps(self.value).encode()

class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "OPENAI_API_KEY": "test-secret",
                "GEMINI_API_KEY": "test-secret",
                "GROQ_API_KEY": "test-secret",
            },
            clear=True,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        limiter = patch("reviso.providers.RATE_LIMITER", RateLimiter(interval=0))
        limiter.start()
        self.addCleanup(limiter.stop)

    def test_connections_never_return_keys_or_assume_free_tier(self):
        value = json.dumps(connections())
        self.assertNotIn("test-secret", value)
        self.assertIn("not inferred", value)

    def test_requests_identify_the_app_for_provider_compatibility(self):
        request = urllib.request.Request(
            "https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer test-secret"}
        )
        with patch("urllib.request.urlopen", return_value=Response({"data": []})) as network:
            self.assertEqual(_read_json(request, 10), {"data": []})
        sent = network.call_args[0][0]
        self.assertEqual(sent.get_header("User-agent"), "AnalyticBrain/0.2")
        self.assertEqual(sent.get_header("Authorization"), "Bearer test-secret")

    def test_openai_responses_contract(self):
        with patch(
            "urllib.request.urlopen",
            return_value=Response(
                {
                    "output": [
                        {"type": "reasoning", "content": []},
                        {"content": [{"type": "output_text", "text": '{"answer":"ok"}'}]},
                    ],
                    "usage": {"input_tokens": 3},
                }
            ),
        ) as network:
            result = Client("openai", "test-model").generate("instruction", {"question": "test"})
            request = network.call_args[0][0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertFalse(payload["store"])
            self.assertEqual(result, {"answer": "ok"})

    def test_gemini_and_groq_chat_contracts(self):
        for provider, base in (
            ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
            ("groq", "https://api.groq.com/openai/v1"),
        ):
            with (
                self.subTest(provider=provider),
                patch(
                    "urllib.request.urlopen",
                    return_value=Response({"choices": [{"message": {"content": '{"ok":true}'}}]}),
                ) as network,
            ):
                self.assertTrue(Client(provider, "test-model").generate("instruction", {})["ok"])
                request = network.call_args[0][0]
                self.assertEqual(request.full_url, base + "/chat/completions")
                payload = json.loads(request.data)
                self.assertEqual(payload["messages"][0]["role"], "system")
                if provider == "groq":
                    self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_groq_native_actions_remain_bounded_and_return_to_the_agent(self):
        context = {"response_kind": "chat", "remaining_tools": 2, "findings": [{"id": "E001"}]}
        reply = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "sql",
                                    "arguments": '{"sql":"SELECT price FROM snapshot_test LIMIT 1","purpose":"Check prices"}',
                                }
                            }
                        ],
                    }
                }
            ]
        }
        with patch("urllib.request.urlopen", return_value=Response(reply)) as network:
            result = Client("groq", "openai/gpt-oss-20b").generate("instruction", context)
        self.assertEqual(result["tool"], "sql")
        payload = json.loads(network.call_args[0][0].data)
        self.assertEqual(payload["tool_choice"], "required")
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertNotIn("response_format", payload)
        tools = {tool["function"]["name"]: tool["function"] for tool in payload["tools"]}
        self.assertEqual(set(tools), {"respond", "sql", "profile"})
        self.assertIn(
            "citation brackets",
            tools["respond"]["parameters"]["properties"]["answer"]["description"],
        )
        context["remaining_tools"] = 0
        reply["choices"][0]["message"]["tool_calls"][0]["function"] = {
            "name": "respond",
            "arguments": '{"answer":"Evidence is available","evidence_ids":["E001"]}',
        }
        with patch("urllib.request.urlopen", return_value=Response(reply)) as network:
            result = Client("groq", "openai/gpt-oss-20b").generate("instruction", context)
        self.assertEqual(result["evidence_ids"], ["E001"])
        self.assertEqual(result["answer"], "Evidence is available [E001]")
        payload = json.loads(network.call_args[0][0].data)
        self.assertEqual([tool["function"]["name"] for tool in payload["tools"]], ["respond"])

    def test_groq_rejects_unknown_parallel_or_invalid_actions(self):
        context = {"response_kind": "chat", "remaining_tools": 0, "findings": []}
        for calls in (
            [],
            [{"function": {"name": "sql", "arguments": "{}"}}],
            [{"function": {"name": "respond", "arguments": "[]"}}],
            [{"function": {"name": "respond", "arguments": "not json"}}],
            [{"function": {"name": "respond", "arguments": '{"answer":"Unsupported","evidence_ids":["Q999"]}'}}],
            [{"function": {"name": "respond", "arguments": "{}"}}] * 2,
        ):
            with (
                self.subTest(calls=calls),
                patch(
                    "urllib.request.urlopen",
                    return_value=Response({"choices": [{"message": {"tool_calls": calls}}]}),
                ),
                self.assertRaises(ProviderError),
            ):
                Client("groq", "openai/gpt-oss-20b").generate("instruction", context)

    def test_no_missing_credentials_or_invented_model_default(self):
        with self.assertRaisesRegex(ProviderError, "model ID"):
            Client("openai")
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ProviderError, "OPENAI_API_KEY"),
        ):
            Client("openai", "test")

    def test_quota_failure_is_recorded_and_body_is_not_exposed(self):
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limit",
            {"Retry-After": "0"},
            io.BytesIO(b"test-secret private dataset echoed"),
        )
        events = []
        with patch("urllib.request.urlopen", side_effect=error), patch("time.sleep"):
            with self.assertRaisesRegex(ProviderError, "quota") as raised:
                Client("groq", "test", lambda k, d: events.append((k, d))).generate(
                    "instruction", {}
                )
        self.assertNotIn("test-secret", str(raised.exception))
        calls = [event for event in events if event[0] == "model_call"]
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(event[1]["status"] == "failed" for event in calls))

    def test_rate_limited_request_waits_then_retries_successfully(self):
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limit",
            {"Retry-After": "12"},
            io.BytesIO(b"private body"),
        )
        events = []
        response = Response({"choices": [{"message": {"content": '{"answer":"ok"}'}}]})
        with (
            patch("urllib.request.urlopen", side_effect=[error, response]) as network,
            patch("time.monotonic", return_value=100),
            patch("time.sleep") as sleep,
        ):
            answer = Client(
                "groq", "test", lambda kind, detail: events.append((kind, detail))
            ).generate("instruction", {})
        self.assertEqual(answer, {"answer": "ok"})
        self.assertEqual(network.call_count, 2)
        sleep.assert_called_once_with(12.25)
        self.assertIn("rate_limit_wait", [event[0] for event in events])
        self.assertEqual(
            [event[1].get("status") for event in events if event[0] == "model_call"],
            ["failed", None],
        )

    def test_malformed_structured_answer_rejected(self):
        for text in ("not json", "[]"):
            with (
                patch(
                    "urllib.request.urlopen",
                    return_value=Response({"choices": [{"message": {"content": text}}]}),
                ),
                self.assertRaises(ProviderError),
            ):
                Client("gemini", "test").generate("instruction", {})

    def test_embedding_results_are_ordered_and_validated(self):
        with patch(
            "urllib.request.urlopen",
            return_value=Response(
                {"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
            ),
        ):
            self.assertEqual(
                Client("openai", "test").embed(["a", "b"], "test-embed"), [[1, 0], [0, 1]]
            )
        with (
            patch(
                "urllib.request.urlopen",
                return_value=Response({"data": [{"index": 0, "embedding": [float("nan")]}]}),
            ),
            self.assertRaises(ProviderError),
        ):
            Client("openai", "test").embed(["a"], "test-embed")

    def test_groq_embedding_requires_separate_connection(self):
        with self.assertRaisesRegex(ProviderError, "separate"):
            Client("groq", "test").embed(["text"], "test-embed")

if __name__ == "__main__":
    unittest.main()
