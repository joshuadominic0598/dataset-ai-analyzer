"""Immutable SQLite snapshots and report-scoped read-only access."""

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

from .csv import parse_csv, profile
from .values import ANALYSIS_DEFAULTS, WRITE_LOCK, dump, now, quote, uid

class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, name TEXT, created_at TEXT, config TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, dataset_id TEXT REFERENCES datasets(id), ordinal INTEGER,
                    label TEXT, captured_at TEXT, digest TEXT, row_count INTEGER,
                    columns_json TEXT, profile_json TEXT, source BLOB, table_name TEXT,
                    UNIQUE(dataset_id, ordinal));
                CREATE TABLE IF NOT EXISTS reports (
                    id TEXT PRIMARY KEY, dataset_id TEXT, created_at TEXT, payload TEXT);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, dataset_id TEXT, kind TEXT, status TEXT,
                    started_at TEXT, finished_at TEXT, elapsed_ms INTEGER, payload TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, dataset_id TEXT, report_id TEXT, role TEXT,
                    content TEXT, created_at TEXT, run_id TEXT);
                CREATE TABLE IF NOT EXISTS vectors (
                    dataset_id TEXT, version_id TEXT, column_name TEXT, row_number INTEGER,
                    provider TEXT, model TEXT, text TEXT, embedding TEXT,
                    PRIMARY KEY(version_id, column_name, row_number, provider, model));
                CREATE TABLE IF NOT EXISTS feedback (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id), rating INTEGER, note TEXT, created_at TEXT);
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def create_dataset(self, name):
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("Dataset names need 1–100 characters.")
        identifier = uid()
        config = {
            "identity": [],
            "important": [],
            "semantic": [],
            "setup_complete": False,
            "cloud_allowed": False,
            "growth_threshold": 25,
            "missing_threshold": 5,
            "context": "",
            "embedding_provider": "",
            "embedding_model": "",
            "rename_decisions": [],
            **ANALYSIS_DEFAULTS,
        }
        with self.db() as db:
            db.execute(
                "INSERT INTO datasets VALUES (?,?,?,?)", (identifier, name, now(), dump(config))
            )
        return self.dataset(identifier)

    def dataset(self, identifier):
        with self.db() as db:
            row = db.execute("SELECT * FROM datasets WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Dataset not found.")
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "config": {**ANALYSIS_DEFAULTS, **json.loads(row["config"])},
        }

    def datasets(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM datasets ORDER BY created_at DESC")]
        return [dict(self.dataset(i), versions=len(self.versions(i))) for i in ids]

    def configure(self, identifier, incoming):
        from .configuration import validate_config

        if not isinstance(incoming, dict):
            raise ValueError("Dataset setup must be a JSON object.")
        dataset = self.dataset(identifier)
        versions = self.versions(identifier)
        columns = {column for version in versions for column in version["columns"]}
        config = validate_config(dataset["config"], incoming, columns)
        with self.db() as db:
            db.execute("UPDATE datasets SET config=? WHERE id=?", (dump(config), identifier))
        return self.dataset(identifier)

    def versions(self, dataset_id):
        self.dataset(dataset_id)
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM versions WHERE dataset_id=? ORDER BY ordinal", (dataset_id,)
            ).fetchall()
        return [
            {
                "id": r["id"],
                "ordinal": r["ordinal"],
                "label": r["label"],
                "captured_at": r["captured_at"],
                "digest": r["digest"],
                "row_count": r["row_count"],
                "columns": json.loads(r["columns_json"]),
                "profile": json.loads(r["profile_json"]),
                "table": r["table_name"],
            }
            for r in rows
        ]

    def ingest(self, dataset_id, label, content):
        header, rows = parse_csv(content)
        facts = profile(header, rows)
        label = str(label).strip()[:100]
        if not label:
            raise ValueError("Give this version a label.")
        with WRITE_LOCK, self.db() as db:
            self.dataset(dataset_id)
            total = db.execute(
                "SELECT count(*) FROM versions WHERE dataset_id=?", (dataset_id,)
            ).fetchone()[0]
            if total >= 5:
                raise ValueError(
                    "This timeline already has five versions. Start another dataset timeline; saved snapshots are never silently removed."
                )
            version_id = uid()
            table = "snapshot_" + version_id
            sql_types = {"integer": "INTEGER", "number": "REAL"}
            db.execute(
                f"CREATE TABLE {quote(table)} ({','.join(quote(c) + ' ' + sql_types.get(facts[c]['kind'], 'TEXT') for c in header)})"
            )

            def typed(row):
                return [
                    None
                    if row[c] is None
                    else int(Decimal(row[c]))
                    if facts[c]["kind"] == "integer"
                    else float(row[c])
                    if facts[c]["kind"] == "number"
                    else row[c]
                    for c in header
                ]

            db.executemany(
                f"INSERT INTO {quote(table)} VALUES ({','.join('?' for _ in header)})",
                (typed(r) for r in rows),
            )
            db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    dataset_id,
                    total + 1,
                    label,
                    now(),
                    hashlib.sha256(content).hexdigest(),
                    len(rows),
                    dump(header),
                    dump(facts),
                    content,
                    table,
                ),
            )
        return self.versions(dataset_id)[-1]

    def source(self, dataset_id, version_id):
        with self.db() as db:
            row = db.execute(
                "SELECT source FROM versions WHERE id=? AND dataset_id=?", (version_id, dataset_id)
            ).fetchone()
        if not row:
            raise ValueError("Version not found in this dataset.")
        return bytes(row[0])

    def rows(self, dataset_id, version):
        return parse_csv(self.source(dataset_id, version["id"]))[1]

    def query(self, dataset_id, sql, version_ids=None):
        if not isinstance(sql, str) or len(sql) > 12_000:
            raise ValueError("Queries must be at most 12,000 characters.")
        tables = {
            v["table"]
            for v in self.versions(dataset_id)
            if version_ids is None or v["id"] in version_ids
        }
        if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            raise ValueError("Only read-only SELECT queries are available.")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2
        safe_functions = {
            "count",
            "sum",
            "avg",
            "min",
            "max",
            "total",
            "round",
            "abs",
            "coalesce",
            "ifnull",
            "nullif",
            "cast",
            "lower",
            "upper",
            "length",
            "substr",
            "substring",
            "trim",
            "ltrim",
            "rtrim",
            "replace",
            "like",
            "glob",
            "date",
            "datetime",
            "strftime",
            "julianday",
            "typeof",
            "row_number",
            "rank",
            "dense_rank",
            "lag",
            "lead",
            "first_value",
            "last_value",
        }

        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if arg1 in tables else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return (
                    sqlite3.SQLITE_OK
                    if (arg2 or "").lower() in safe_functions
                    else sqlite3.SQLITE_DENY
                )
            if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        try:
            db.set_authorizer(authorize)
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 2000)
            cursor = db.execute(sql)
            result = cursor.fetchmany(201)
            names = [c[0] for c in cursor.description]
            rows = [[v[:2000] if isinstance(v, str) else v for v in tuple(r)] for r in result[:200]]
            payload = {
                "columns": names,
                "rows": rows,
                "truncated": len(result) > 200,
                "row_limit": 200,
            }
            if len(dump(payload)) > 500_000:
                raise ValueError("Query results are too wide. Select fewer columns or records.")
            return payload
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise ValueError(f"Query could not run: {exc}") from exc
        finally:
            db.close()

    def save_report(self, dataset_id, report):
        with self.db() as db:
            db.execute(
                "INSERT INTO reports VALUES (?,?,?,?)",
                (report["id"], dataset_id, report["created_at"], dump(report)),
            )

    def report(self, dataset_id, report_id=None):
        self.dataset(dataset_id)
        with self.db() as db:
            row = db.execute(
                "SELECT payload FROM reports WHERE dataset_id=? "
                + ("AND id=? " if report_id else "")
                + "ORDER BY created_at DESC LIMIT 1",
                (dataset_id, report_id) if report_id else (dataset_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            feedback = db.execute(
                "SELECT rating,note FROM feedback WHERE run_id=?", (run_id,)
            ).fetchone()
        if not row:
            raise ValueError("Run not found.")
        return dict(
            row, payload=json.loads(row["payload"]), feedback=dict(feedback) if feedback else None
        )

    def runs(self, dataset_id):
        with self.db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM runs WHERE dataset_id=? ORDER BY started_at DESC LIMIT 50",
                    (dataset_id,),
                )
            ]
        return [self.run(i) for i in ids]

    def history(self, dataset_id, report_id):
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE dataset_id=? AND report_id=? ORDER BY created_at DESC LIMIT 12",
                (dataset_id, report_id),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recover_interrupted_runs(self):
        with self.db() as db:
            rows = db.execute("SELECT id,payload FROM runs WHERE status='running'").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                payload["error"] = (
                    "The server stopped before this run finished. Start a new investigation."
                )
                db.execute(
                    "UPDATE runs SET status='failed',finished_at=?,payload=? WHERE id=?",
                    (now(), dump(payload), row["id"]),
                )

    def message(self, dataset_id, report_id, role, content, run_id):
        with self.db() as db:
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?)",
                (uid(), dataset_id, report_id, role, content, now(), run_id),
            )

    def save_feedback(self, dataset_id, run_id, rating, note=""):
        run = self.run(run_id)
        if run["dataset_id"] != dataset_id or rating not in (-1, 1):
            raise ValueError("Choose useful or needs work for a run in this dataset.")
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO feedback VALUES (?,?,?,?)",
                (run_id, rating, str(note)[:500], now()),
            )
        return {"saved": True}
