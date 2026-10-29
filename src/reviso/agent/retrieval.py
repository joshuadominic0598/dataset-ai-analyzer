"""Opt-in text indexing and report-scoped semantic retrieval."""

import json
import math

from ..data import dump
from ..providers import Client

def index_status(store, report):
    config = report["config"]
    with store.db() as db:
        records = db.execute(
            "SELECT version_id, column_name, count(*) AS n FROM vectors WHERE dataset_id=? AND provider=? AND model=? GROUP BY version_id,column_name",
            (report["dataset_id"], config["embedding_provider"], config["embedding_model"]),
        ).fetchall()
    counts = {(r["version_id"], r["column_name"]): r["n"] for r in records}
    expected = [
        (v["id"], c, v["row_count"] - v["profile"][c]["missing"])
        for v in report["versions"]
        for c in config["semantic"]
        if c in v["columns"]
    ]
    return {
        "ready": bool(expected) and all(counts.get((v, c), 0) == n for v, c, n in expected),
        "indexed": sum(counts.get((v, c), 0) for v, c, _ in expected),
        "expected": sum(n for _, _, n in expected),
    }

def build_index(store, dataset_id, trace, client_factory=Client):
    report = store.report(dataset_id)
    if not report:
        raise ValueError("Create a report before building text search.")
    config = report["config"]
    current = store.dataset(dataset_id)["config"]
    if not config["semantic"] or (
        config["embedding_provider"] != "ollama"
        and (not config["cloud_allowed"] or not current["cloud_allowed"])
    ):
        raise ValueError(
            "Select text columns and allow cloud analysis in setup, then create a new report."
        )
    provider, model = config["embedding_provider"], config["embedding_model"]
    client = client_factory(provider, model, trace.event)
    expected = index_status(store, report)["expected"]
    if expected > 5000:
        raise ValueError(
            "This first version supports up to 5,000 text cells per report index. Select fewer text columns or smaller snapshots."
        )
    completed = 0
    for version in report["versions"]:
        rows = store.rows(dataset_id, version)
        for col in config["semantic"]:
            if col not in version["columns"]:
                continue
            with store.db() as db:
                existing = {
                    r[0]
                    for r in db.execute(
                        "SELECT row_number FROM vectors WHERE version_id=? AND column_name=? AND provider=? AND model=?",
                        (version["id"], col, provider, model),
                    )
                }
            pending = [
                (i, r[col])
                for i, r in enumerate(rows, 1)
                if r[col] is not None and i not in existing
            ]
            for start in range(0, len(pending), 24):
                batch = pending[start : start + 24]
                if any(len(text) > 6000 for _, text in batch):
                    raise ValueError(
                        "A selected text cell exceeds 6,000 characters. Shorten it or exclude that column."
                    )
                vectors = client.embed([text for _, text in batch], model)
                with store.db() as db:
                    db.executemany(
                        "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?,?)",
                        [
                            (dataset_id, version["id"], col, i, provider, model, text, dump(vec))
                            for (i, text), vec in zip(batch, vectors)
                        ],
                    )
                completed += len(batch)
                trace.event(
                    "embed",
                    {
                        "version": version["ordinal"],
                        "column": col,
                        "cells_this_run": completed,
                        "expected": expected,
                    },
                )
    return index_status(store, report)

def semantic_search(store, report, query, ordinal, trace, client_factory=Client):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ValueError("Use a text-search query between 1 and 2,000 characters.")
    if not index_status(store, report)["ready"]:
        raise ValueError("The selected text index is not complete.")
    config = report["config"]
    if config["embedding_provider"] != "ollama" and (
        not config["cloud_allowed"]
        or not store.dataset(report["dataset_id"])["config"]["cloud_allowed"]
    ):
        raise ValueError("Cloud analysis permission has been revoked for this dataset.")
    version = next((v for v in report["versions"] if v["ordinal"] == ordinal), None)
    if not version:
        raise ValueError("Choose a version in this report.")
    client = client_factory(config["embedding_provider"], config["embedding_model"], trace.event)
    vector = client.embed([query], config["embedding_model"])[0]
    norm = math.sqrt(sum(n * n for n in vector))
    with store.db() as db:
        rows = db.execute(
            "SELECT * FROM vectors WHERE version_id=? AND provider=? AND model=?",
            (version["id"], config["embedding_provider"], config["embedding_model"]),
        ).fetchall()
    matches = []
    for row in rows:
        if row["column_name"] not in config["semantic"]:
            continue
        other = json.loads(row["embedding"])
        if len(other) != len(vector):
            raise ValueError("The embedding dimensions changed. Use a new model ID and rebuild.")
        denom = norm * math.sqrt(sum(n * n for n in other))
        score = sum(a * b for a, b in zip(vector, other)) / denom if denom else 0
        matches.append(
            {
                "version": ordinal,
                "row": row["row_number"],
                "column": row["column_name"],
                "text": row["text"],
                "similarity": round(score, 5),
            }
        )
    matches.sort(key=lambda v: -v["similarity"])
    result = {
        "matches": matches[:6],
        "searched_cells": len(matches),
        "meaning": "Cosine similarity ranks text relevance; it does not measure factual accuracy.",
    }
    trace.event("semantic_search", {"query": query, "version": ordinal, "result": result})
    return result
