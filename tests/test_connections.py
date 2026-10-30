import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reviso.connections import ConnectionManager, local_url
from reviso.providers import Client, ProviderError, list_models

class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "connections.json"
        self.manager = ConnectionManager(self.path)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_four_connections_and_qwen_default(self):
        choices = self.manager.public()
        self.assertEqual({c["id"] for c in choices}, {"ollama", "gemini", "openai", "groq"})
        local = next(c for c in choices if c["id"] == "ollama")
        self.assertEqual(local["model"], "qwen3:8b")
        self.assertTrue(local["configured"])
        self.assertEqual(local["credential_source"], "not_required")

    def test_saved_keys_are_private_and_not_returned(self):
        result = self.manager.save(
            "gemini", {"api_key": "private-test-key", "tier": "free", "model": "gemini-test"}
        )
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn("private-test-key", json.dumps(result))
        self.assertEqual(self.manager.resolve("gemini")["api_key"], "private-test-key")
        self.assertEqual(next(c for c in result if c["id"] == "gemini")["tier"], "free")

    def test_blank_key_preserves_existing_and_forget_removes_it(self):
        self.manager.save("groq", {"api_key": "saved-test-key", "tier": "free"})
        self.manager.save("groq", {"api_key": "", "model": "test-model"})
        self.assertEqual(self.manager.resolve("groq")["api_key"], "saved-test-key")
        self.manager.save("groq", {"forget_key": True})
        self.assertEqual(self.manager.resolve("groq")["api_key"], "")

    def test_environment_keys_remain_available_after_saved_key_removed(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "env-secret"}):
            self.manager.save("openai", {"api_key": "saved-secret", "tier": "paid"})
            self.assertEqual(self.manager.resolve("openai")["api_key"], "saved-secret")
            self.manager.save("openai", {"forget_key": True})
            self.assertEqual(self.manager.resolve("openai")["api_key"], "env-secret")
            self.assertEqual(self.manager.resolve("openai")["credential_source"], "environment")

    def test_free_and_paid_gemini_profiles_use_same_adapter(self):
        for tier in ("free", "paid"):
            self.manager.save(
                "gemini", {"api_key": "test-key", "tier": tier, "model": "test-model"}
            )
            client = self.manager.client_factory()("gemini")
            self.assertEqual(client.base, "https://generativelanguage.googleapis.com/v1beta/openai")
            self.assertEqual(client.model, "test-model")

    def test_invalid_keys_tiers_and_models_rejected(self):
        for settings in (
            {"api_key": "key\nInjected: header"},
            {"api_key": 123},
            {"tier": "unlimited"},
            {"model": "model with spaces"},
            {"model": None},
            {"forget_key": "false"},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                self.manager.save("groq", settings)

    def test_local_endpoints_cannot_be_remote_or_contain_credentials(self):
        for value in (
            "http://remote.example:11434",
            "http://127.0.0.1.evil.test",
            "http://user:secret@localhost:11434",
            "file:///tmp/file",
            "http://127.0.0.1:11434/path",
            "http://localhost:11434?redirect=evil",
            "http://localhost:99999",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_url(value)
        self.assertEqual(local_url("http://localhost:11434/"), "http://localhost:11434")
        self.assertEqual(local_url("http://[::1]:11434"), "http://[::1]:11434")

    def test_run_factory_pins_credentials_and_model_defaults(self):
        self.manager.save("openai", {"api_key": "first-key", "model": "first-model"})
        factory = self.manager.client_factory()
        self.manager.save("openai", {"api_key": "second-key", "model": "second-model"})
        self.assertEqual(factory("openai").key, "first-key")
        self.assertEqual(factory("openai").model, "first-model")
        self.assertEqual(self.manager.client_factory()("openai").key, "second-key")

    def test_ollama_needs_no_key_and_refuses_cloud_tags(self):
        self.assertEqual(Client("ollama").model, "qwen3:8b")
        with self.assertRaisesRegex(ProviderError, "Cloud-backed"):
            Client("ollama", "model:cloud")

    def test_native_ollama_payload_and_reported_usage(self):
        events = []
        with patch(
            "reviso.providers._read_json",
            side_effect=[
                {"capabilities": ["completion", "thinking"]},
                {
                    "message": {"content": '{"status":"ok"}', "thinking": "private reasoning"},
                    "prompt_eval_count": 21,
                    "eval_count": 7,
                },
            ],
        ) as network:
            answer = Client("ollama", recorder=lambda kind, detail: events.append(detail)).generate(
                "Return JSON.", {"task": "test"}
            )
        self.assertEqual(answer, {"status": "ok"})
        request = network.call_args_list[1].args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/chat")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["format"], "json")
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(events[0]["usage"]["total_tokens"], 28)
        self.assertNotIn("private reasoning", json.dumps(events))

    def test_remote_ollama_alias_rejected_before_generation_or_embedding(self):
        for kind in ("generate", "embed"):
            with patch(
                "reviso.providers._read_json",
                return_value={"remote_host": "https://remote.example"},
            ) as network:
                client = Client("ollama", "innocent-local-alias")
                with self.assertRaisesRegex(ProviderError, "remote|local embedding"):
                    if kind == "generate":
                        client.generate("Return JSON.", {})
                    else:
                        client.embed(["private description"], "innocent-local-alias")
                self.assertEqual(network.call_count, 1)

    def test_ollama_embedding_contract(self):
        with patch(
            "reviso.providers._read_json",
            side_effect=[{"capabilities": ["embedding"]}, {"embeddings": [[1, 0], [0, 1]]}],
        ) as network:
            result = Client("ollama", "embedding-test").embed(["a", "b"], "embedding-test")
        self.assertEqual(result, [[1, 0], [0, 1]])
        request = network.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:11434/api/embed")
        self.assertFalse(json.loads(request.data)["truncate"])

    def test_ollama_catalog_filters_embedding_and_cloud_entries(self):
        payload = {
            "models": [
                {"name": "qwen3:8b"},
                {"name": "llama3:8b"},
                {"name": "embeddinggemma:latest"},
                {"name": "cloud-model:cloud"},
                {"name": "alias", "remote_host": "https://example.test"},
            ]
        }
        with patch("reviso.providers._read_json", return_value=payload):
            result = list_models("ollama")
        self.assertEqual([m["id"] for m in result["models"]], ["qwen3:8b", "llama3:8b"])

    def test_gemini_catalog_filters_by_capability_and_paginates(self):
        pages = [
            {
                "models": [
                    {
                        "name": "models/gemini-test-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embedding-test",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ],
                "nextPageToken": "page-two",
            },
            {
                "models": [
                    {
                        "name": "models/gemini-test-pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/gemini-test-image",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            },
        ]
        with patch("reviso.providers._read_json", side_effect=pages) as network:
            result = list_models("gemini", {"api_key": "catalog-key"})
        self.assertEqual(len(result["models"]), 2)
        request = network.call_args_list[0].args[0]
        self.assertNotIn("catalog-key", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "catalog-key")
        self.assertIn("pageToken=page-two", network.call_args_list[1].args[0].full_url)

    def test_openai_and_groq_catalogs_exclude_non_text_models(self):
        for provider, items in [
            (
                "openai",
                [
                    {"id": "gpt-test-mini"},
                    {"id": "gpt-test-pro"},
                    {"id": "gpt-audio"},
                    {"id": "text-embedding-test"},
                ],
            ),
            (
                "groq",
                [
                    {"id": "llama-test", "active": True},
                    {"id": "qwen-test", "active": True},
                    {"id": "whisper-test"},
                    {"id": "canopylabs/orpheus-v1-english"},
                    {"id": "retired", "active": False},
                ],
            ),
        ]:
            with (
                self.subTest(provider=provider),
                patch("reviso.providers._read_json", return_value={"data": items}),
            ):
                result = list_models(provider, {"api_key": "catalog-key"})
                self.assertEqual(len(result["models"]), 2)
                self.assertNotIn("catalog-key", json.dumps(result))

    def test_corrupt_settings_are_not_silently_overwritten(self):
        self.path.write_text("broken json")
        with self.assertRaisesRegex(ValueError, "could not be read"):
            self.manager.save("ollama", {"model": "qwen3:8b"})
        self.assertEqual(self.path.read_text(), "broken json")

if __name__ == "__main__":
    unittest.main()
