"""Local connection preferences. Secrets never appear in public connection metadata."""

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

MODEL_ID = re.compile(r"[\w./:\-]+\Z")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SPECS = {
    "ollama": {"name": "Ollama", "model": "OLLAMA_MODEL", "default": "qwen3:8b", "key": None, "embeddings": True},
    "gemini": {"name": "Gemini", "model": "GEMINI_MODEL", "default": "", "key": "GEMINI_API_KEY", "embeddings": True},
    "openai": {"name": "OpenAI", "model": "OPENAI_MODEL", "default": "", "key": "OPENAI_API_KEY", "embeddings": True},
    "groq": {"name": "Groq", "model": "GROQ_MODEL", "default": "", "key": "GROQ_API_KEY", "embeddings": False},
}


def local_url(value):
    value = str(value).strip().rstrip("/")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Use a valid local Ollama port.") from None
    if (parsed.scheme not in ("http", "https") or parsed.hostname not in LOCAL_HOSTS
            or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path
            or (port is not None and not 1 <= port <= 65535)):
        raise ValueError("Ollama must use a loopback address such as http://127.0.0.1:11434.")
    return value


def valid_model(value):
    return isinstance(value, str) and len(value) <= 120 and bool(MODEL_ID.fullmatch(value))


class ConnectionManager:
    def __init__(self, path=None):
        self.path = Path(path).resolve() if path else None
        self.lock = threading.RLock()

    def _read(self):
        if not self.path or not self.path.exists():
            return {}
        try:
            content = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(content, dict) or not isinstance(content.get("connections"), dict):
                raise ValueError()
            return content["connections"]
        except (ValueError, OSError):
            raise ValueError("The local connections file could not be read. Restore a valid copy before saving changes.") from None

    def resolve(self, provider):
        if provider not in SPECS:
            raise ValueError("Choose Ollama, Gemini, OpenAI, or Groq.")
        spec = SPECS[provider]
        with self.lock:
            saved = self._read().get(provider, {})
        if not isinstance(saved, dict):
            raise ValueError("The saved connection is invalid.")
        key = saved.get("api_key") or (os.environ.get(spec["key"], "") if spec["key"] else "")
        source = "saved" if saved.get("api_key") else "environment" if key else "not_required" if provider == "ollama" else "missing"
        return {"api_key": key, "credential_source": source,
                "model": saved.get("model") or os.environ.get(spec["model"]) or spec["default"],
                "tier": saved.get("tier", "local" if provider == "ollama" else "auto"),
                "base_url": local_url(saved.get("base_url") or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")) if provider == "ollama" else ""}

    def public(self):
        result = []
        for provider, spec in SPECS.items():
            connection = self.resolve(provider)
            local = provider == "ollama"
            result.append({"id": provider, "name": spec["name"], "configured": local or bool(connection["api_key"]),
                           "model": connection["model"], "tier": connection["tier"], "base_url": connection["base_url"],
                           "credential_source": connection["credential_source"], "embeddings": spec["embeddings"], "local": local,
                           "billing": "Local inference; no provider token charges. Your hardware costs remain." if local else
                                      "Determined by your provider account; not inferred or changed by this setting."})
        return result

    def save(self, provider, incoming):
        if provider not in SPECS or not isinstance(incoming, dict):
            raise ValueError("Choose a supported connection and its settings.")
        if not self.path:
            raise ValueError("Saving connections is unavailable in this context.")
        with self.lock:
            connections = self._read()
            saved = dict(connections.get(provider, {}))
            model = incoming.get("model", saved.get("model", ""))
            if not isinstance(model, str) or (model and not valid_model(model)):
                raise ValueError("Enter a valid default model ID.")
            saved["model"] = model
            tier = incoming.get("tier", saved.get("tier", "auto"))
            if provider == "ollama":
                saved["base_url"] = local_url(incoming.get("base_url", saved.get("base_url", "http://127.0.0.1:11434")))
                saved["tier"] = "local"
                saved.pop("api_key", None)
            else:
                if tier not in ("auto", "free", "paid") or (provider == "openai" and tier == "free"):
                    raise ValueError("Choose an applicable account tier.")
                saved["tier"] = tier
                key = incoming.get("api_key", "")
                if "forget_key" in incoming and not isinstance(incoming["forget_key"], bool):
                    raise ValueError("Key removal must be true or false.")
                if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    raise ValueError("The API key contains invalid characters.")
                if incoming.get("forget_key"):
                    saved.pop("api_key", None)
                elif key:
                    saved["api_key"] = key
            connections[provider] = saved
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".connections-", dir=self.path.parent)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"version": 1, "connections": connections}, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return self.public()

    def client_factory(self):
        # Pin credentials/defaults for an entire run, without persisting them in its trace.
        from .providers import Client
        settings = {provider: self.resolve(provider) for provider in SPECS}
        return lambda provider, model=None, recorder=None: Client(provider, model, recorder, settings=settings[provider])
