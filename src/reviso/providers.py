"""Small provider boundary: explicit connections, timeouts, and sanitized failures."""

import json
import re
import time
import urllib.error
import urllib.request

from .connections import ConnectionManager, local_url, valid_model
from .data import dump
from .structured import response_schema

PROVIDERS = {
    "ollama": {"base": "http://127.0.0.1:11434", "key": None, "model": "OLLAMA_MODEL"},
    "openai": {
        "base": "https://api.openai.com/v1",
        "key": "OPENAI_API_KEY",
        "model": "OPENAI_MODEL",
    },
    "gemini": {
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key": "GEMINI_API_KEY",
        "model": "GEMINI_MODEL",
    },
    "groq": {
        "base": "https://api.groq.com/openai/v1",
        "key": "GROQ_API_KEY",
        "model": "GROQ_MODEL",
    },
}

class ProviderError(ValueError):
    pass

def connections(manager=None):
    return (manager or ConnectionManager()).public()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _read_json(request, timeout, local=False):
    if local:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        response = opener.open(request, timeout=timeout)
    else:
        response = urllib.request.urlopen(request, timeout=timeout)
    with response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ProviderError("Provider response exceeded the size limit.")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ProviderError("The provider response must be a JSON object.")
    return result

def _cloud_model(name, item=None):
    return bool(
        re.search(r"(?:^|[-:])cloud(?:$|[-:])", name, re.I)
        or (item or {}).get("remote_host")
        or (item or {}).get("remote_model")
    )

def list_models(provider, settings=None):
    if provider not in PROVIDERS:
        raise ProviderError("Choose a supported provider.")
    settings = settings or ConnectionManager().resolve(provider)
    key = settings.get("api_key", "")
    if provider != "ollama" and not key:
        raise ProviderError(
            "Save an API key or configure it in the server environment before refreshing models."
        )
    headers = {}
    if provider == "gemini":
        url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
        headers["x-goog-api-key"] = key
    elif provider == "ollama":
        url = local_url(settings.get("base_url", "http://127.0.0.1:11434")) + "/api/tags"
    else:
        url = PROVIDERS[provider]["base"] + "/models"
        headers["Authorization"] = "Bearer " + key
    result, pages = [], 0
    try:
        while url and pages < 5:
            payload = _read_json(
                urllib.request.Request(url, headers=headers), 10, provider == "ollama"
            )
            items = payload.get("models" if provider in ("gemini", "ollama") else "data", [])
            if not isinstance(items, list):
                raise ProviderError("The provider returned an invalid model list.")
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name" if provider in ("gemini", "ollama") else "id", ""))
                if provider == "gemini":
                    name = name.removeprefix("models/")
                    if "generateContent" not in item.get(
                        "supportedGenerationMethods", []
                    ) or not name.startswith("gemini-"):
                        continue
                if not valid_model(name):
                    continue
                if provider == "ollama":
                    if _cloud_model(name, item) or any(
                        s in name.lower() for s in ("embed", "nomic", "mxbai", "bge-")
                    ):
                        continue
                elif provider == "openai":
                    if not name.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
                        continue
                elif provider == "groq" and item.get("active") is False:
                    continue
                if provider != "ollama" and any(
                    s in name.lower()
                    for s in (
                        "whisper",
                        "tts",
                        "transcri",
                        "realtime",
                        "image",
                        "audio",
                        "speech",
                        "guard",
                        "embedding",
                        "moderation",
                        "live-",
                        "research",
                        "robotics",
                    )
                ):
                    continue
                result.append(
                    {
                        "id": name,
                        "label": str(item.get("displayName") or name),
                        "installed": provider == "ollama",
                        "size_bytes": item.get("size") if provider == "ollama" else None,
                    }
                )
            pages += 1
            token = payload.get("nextPageToken") if provider == "gemini" else None
            if token:
                from urllib.parse import urlencode

                url = "https://generativelanguage.googleapis.com/v1beta/models?" + urlencode(
                    {"pageSize": 1000, "pageToken": token}
                )
            else:
                url = None
        unique = {item["id"]: item for item in result}
        result = sorted(
            unique.values(), key=lambda item: (item["id"] != settings.get("model"), item["id"])
        )
        return {
            "provider": provider,
            "models": result,
            "truncated": bool(url),
            "source": "installed" if provider == "ollama" else "provider_api",
            "note": "Installed local text models; cloud-backed models are excluded."
            if provider == "ollama"
            else "Live text-model catalog. Model listing does not prove free-tier eligibility or successful generation; your account controls access and billing.",
        }
    except urllib.error.HTTPError as exc:
        message = (
            "The provider rejected the key or model-list request."
            if exc.code in (401, 403)
            else "Provider quota or rate limit reached."
            if exc.code == 429
            else "The model list could not be loaded."
        )
        raise ProviderError(f"{message} HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ProviderError(
            "Ollama is not reachable. Start Ollama and refresh installed models."
            if provider == "ollama"
            else "Could not reach the provider to load models."
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ProviderError("The provider returned an invalid model-list response.") from None

class Client:
    def __init__(self, provider, model=None, recorder=None, settings=None):
        if provider not in PROVIDERS:
            raise ProviderError("Choose Ollama, OpenAI, Gemini, or Groq.")
        self.provider = provider
        self.settings = PROVIDERS[provider]
        connection = settings if settings is not None else ConnectionManager().resolve(provider)
        self.key = connection.get("api_key", "")
        self.model = str(model or connection.get("model", "")).strip()
        self.base = (
            local_url(connection.get("base_url", "http://127.0.0.1:11434"))
            if provider == "ollama"
            else self.settings["base"]
        )
        self.local_verified = False
        self.capabilities = []
        self.investigation = None
        self.recorder = recorder
        if provider != "ollama" and not self.key:
            raise ProviderError(f"Set {self.settings['key']} in the server environment.")
        if not valid_model(self.model):
            raise ProviderError("Enter a valid model ID available to your provider account.")
        if provider == "ollama" and _cloud_model(self.model):
            raise ProviderError(
                "Choose an installed local Ollama model. Cloud-backed Ollama models are not enabled."
            )

    def request(self, endpoint, payload, record=True):
        headers = {"Content-Type": "application/json"}
        if self.provider != "ollama":
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(
            self.base + endpoint, data=dump(payload).encode(), headers=headers
        )
        start = time.monotonic()
        for attempt in range(2):
            try:
                result = _read_json(
                    request, 180 if self.provider == "ollama" else 35, self.provider == "ollama"
                )
                usage = result.get("usage")
                if (
                    self.provider == "ollama"
                    and "prompt_eval_count" in result
                    and "eval_count" in result
                ):
                    usage = {
                        "input_tokens": result["prompt_eval_count"],
                        "output_tokens": result["eval_count"],
                        "total_tokens": result["prompt_eval_count"] + result["eval_count"],
                    }
                if self.recorder and record:
                    self.recorder(
                        "model_call",
                        {
                            "provider": self.provider,
                            "model": payload["model"],
                            "endpoint": endpoint,
                            "elapsed_ms": round((time.monotonic() - start) * 1000),
                            "attempts": attempt + 1,
                            "input_characters": len(dump(payload)),
                            "usage": usage,
                            "cost_usd": 0 if self.provider == "ollama" else None,
                        },
                    )
                return result
            except urllib.error.HTTPError as exc:
                if self.recorder and record:
                    self.recorder(
                        "model_call",
                        {
                            "provider": self.provider,
                            "model": payload["model"],
                            "endpoint": endpoint,
                            "status": "failed",
                            "http_status": exc.code,
                            "attempt": attempt + 1,
                            "elapsed_ms": round((time.monotonic() - start) * 1000),
                            "usage": None,
                            "cost_usd": None,
                        },
                    )
                # Never copy upstream bodies: they can echo credentials or dataset text.
                if exc.code == 429 and attempt == 0:
                    try:
                        delay = min(max(float(exc.headers.get("Retry-After", 1)), 0), 3)
                    except (ValueError, TypeError):
                        delay = 1
                    time.sleep(delay)
                    continue
                message = {
                    401: "The provider rejected this key.",
                    403: "This connection or model is not permitted.",
                    404: "This model ID or endpoint is unavailable.",
                    429: "Provider quota or rate limit reached. Try again later or choose another connection.",
                }.get(exc.code, "The provider request failed.")
                if self.provider == "ollama" and exc.code == 404:
                    message = "The local model is unavailable. Install it with Ollama and refresh the model list."
                raise ProviderError(f"{message} HTTP {exc.code}.") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if self.recorder and record:
                    self.recorder(
                        "model_call",
                        {
                            "provider": self.provider,
                            "model": payload["model"],
                            "endpoint": endpoint,
                            "status": "failed",
                            "attempt": attempt + 1,
                            "usage": None,
                            "cost_usd": None,
                        },
                    )
                raise ProviderError(
                    "Ollama is not reachable or local inference exceeded its time limit."
                    if self.provider == "ollama"
                    else "Could not reach the provider within the request budget."
                ) from None
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise ProviderError("The provider returned an invalid JSON response.") from None

    def configure_investigation(self, settings):
        self.investigation = settings

    def generate(self, instructions, context):
        content = dump(context)
        execution = self.investigation
        if len(content) > 100_000 or (
            execution
            and len(content.encode("utf-8")) + len(instructions.encode("utf-8"))
            > execution["input_bytes"] + execution.get("instruction_bytes", 1800)
        ):
            raise ProviderError("The selected evidence exceeded the model context budget.")
        output = execution["output_tokens"] if execution else 3000
        effort = execution["effort"] if execution else "low"
        if self.provider == "ollama":
            if not self.local_verified:
                metadata = self.request("/api/show", {"model": self.model}, record=False)
                if _cloud_model(self.model, metadata):
                    raise ProviderError(
                        "This Ollama model runs remotely. Choose a local model instead."
                    )
                if metadata.get("capabilities") and "completion" not in metadata["capabilities"]:
                    raise ProviderError(
                        "This Ollama model cannot generate answers. Choose a local text-generation model."
                    )
                self.local_verified = True
                self.capabilities = metadata.get("capabilities", [])
            payload = {
                "model": self.model,
                "stream": False,
                "format": response_schema(context),
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                "options": {
                    "num_ctx": execution["context_window"] if execution else 32768,
                    "num_predict": output,
                    "temperature": 0,
                },
            }
            if "thinking" in self.capabilities:
                payload["think"] = effort == "high"
            result = self.request("/api/chat", payload)
            text = result.get("message", {}).get("content")
        elif self.provider == "openai":
            payload = {
                "model": self.model,
                "instructions": instructions,
                "input": content,
                "max_output_tokens": output,
                "store": False,
            }
            if execution and re.match(r"^(?:gpt-[56](?:[.-]|$)|o[134](?:-|$))", self.model):
                payload["reasoning"] = {"effort": effort}
            result = self.request("/responses", payload)
            text = "".join(
                c.get("text", "")
                for item in result.get("output", [])
                for c in item.get("content", [])
                if c.get("type") == "output_text"
            )
        else:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
                "max_completion_tokens": output,
            }
            if execution and (
                (
                    self.provider == "gemini"
                    and re.match(r"^gemini-(?:2\.5|3(?:[.-]|$))", self.model)
                )
                or (self.provider == "groq" and "gpt-oss" in self.model)
            ):
                payload["reasoning_effort"] = effort
            result = self.request("/chat/completions", payload)
            try:
                text = result["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                raise ProviderError("The model did not return a completed answer.") from None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(
                "The model returned no answer text. Try another model or increase its output allowance."
            )
        try:
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            value = json.loads(text)
        except json.JSONDecodeError:
            raise ProviderError(
                "The model returned an invalid structured answer. No generated claims were published."
            ) from None
        if not isinstance(value, dict):
            raise ProviderError("The model answer must be a JSON object.")
        return value

    def embed(self, texts, model):
        if self.provider == "groq":
            raise ProviderError(
                "Use a separate OpenAI or Gemini embedding connection with Groq chat."
            )
        if not model:
            raise ProviderError("Enter an embedding model ID.")
        if self.provider == "ollama":
            metadata = self.request("/api/show", {"model": model}, record=False)
            if _cloud_model(model, metadata):
                raise ProviderError("Choose a local embedding model.")
        result = self.request(
            "/api/embed" if self.provider == "ollama" else "/embeddings",
            {
                "model": model,
                "input": texts,
                **(
                    {"truncate": False}
                    if self.provider == "ollama"
                    else {"encoding_format": "float"}
                ),
            },
        )
        try:
            if self.provider == "ollama":
                vectors = result["embeddings"]
            else:
                items = sorted(result["data"], key=lambda item: item["index"])
                vectors = [item["embedding"] for item in items]
            import math

            if len(vectors) != len(texts) or any(
                not v
                or len(v) > 8192
                or any(not isinstance(n, (int, float)) or not math.isfinite(n) for n in v)
                for v in vectors
            ):
                raise ValueError()
            if len({len(v) for v in vectors}) != 1:
                raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError):
            raise ProviderError("The embedding provider returned invalid vectors.") from None
