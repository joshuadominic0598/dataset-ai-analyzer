"""Persistent operational traces without private model reasoning."""

import time

from ..data import dump, now, uid
from .prompts import PROMPT_HASH, PROMPT_VERSION

class Trace:
    def __init__(self, store, dataset_id, kind, provider, model, input_text=""):
        self.store, self.id, self.dataset_id = store, uid(), dataset_id
        self.started = time.monotonic()
        self.payload = {
            "provider": provider,
            "model": model,
            "input": input_text,
            "events": [],
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": PROMPT_HASH,
            "cost_usd": 0 if provider == "ollama" else None,
            "cost_note": "Local inference; no provider token charge. Hardware costs are excluded."
            if provider == "ollama"
            else "Not priced; provider account tier and rates have not been configured.",
        }
        with store.db() as db:
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                (self.id, dataset_id, kind, "running", now(), None, None, dump(self.payload)),
            )

    def event(self, kind, detail):
        self.payload["events"].append({"at": now(), "kind": kind, "detail": detail})
        self.persist()

    def persist(self):
        with self.store.db() as db:
            db.execute("UPDATE runs SET payload=? WHERE id=?", (dump(self.payload), self.id))

    def finish(self, result=None, error=None):
        if error:
            self.payload["error"] = str(error)
        self.payload["result"] = result
        with self.store.db() as db:
            db.execute(
                "UPDATE runs SET status=?, finished_at=?, elapsed_ms=?, payload=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    now(),
                    round((time.monotonic() - self.started) * 1000),
                    dump(self.payload),
                    self.id,
                ),
            )
