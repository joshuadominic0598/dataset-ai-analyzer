"""Immutable CSV snapshots, local metadata, and a bounded read-only query surface."""

import csv
import hashlib
import io
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
MAX_COLUMNS = 200
MAX_CELL_CHARS = 20_000
ANALYSIS_DEFAULTS = {"dataset_type": "unknown", "analysis_goal": "", "row_meaning": "", "target_column": "", "date_column": "",
                     "date_format": "auto", "time_grain": "month", "column_roles": {}, "category_orders": {}}
WRITE_LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def canonical(value):
    if value is None or not str(value).strip():
        return None
    value = str(value).strip()
    # Codes such as 001 must not collapse into 1.
    if re.match(r"^[+-]?0\d+", value):
        return value
    try:
        number = Decimal(value)
        if number.is_finite():
            return str(number.normalize()) if number else "0"
    except InvalidOperation:
        pass
    return value


def numeric(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_csv(content):
    if len(content) > MAX_BYTES:
        raise ValueError("Each snapshot must be at most 20 MB.")
    try:
        decoded = content.decode("utf-8-sig")
        if any(ord(c) < 32 and c not in "\t\n\r" for c in decoded):
            raise ValueError("CSV files must contain text without binary control characters.")
        reader = csv.reader(io.StringIO(decoded), strict=True)
        header = next(reader)
        if not header or len(header) > MAX_COLUMNS:
            raise ValueError("Use between 1 and 200 columns.")
        header = [v.strip() for v in header]
        if any(not v or len(v) > 120 or any(ord(c) < 32 for c in v) for v in header):
            raise ValueError("Column names must be nonempty, printable, and at most 120 characters.")
        if len({v.casefold() for v in header}) != len(header):
            raise ValueError("Repeated column names are ambiguous. Rename them before importing.")
        rows = []
        for line, values in enumerate(reader, 2):
            if not values:
                continue
            if len(values) != len(header):
                raise ValueError(f"CSV record {line} has {len(values)} cells; expected {len(header)}.")
            if any(len(v) > MAX_CELL_CHARS for v in values):
                raise ValueError(f"CSV record {line} has a cell longer than 20,000 characters.")
            rows.append(dict(zip(header, [v if v.strip() else None for v in values])))
            if len(rows) > MAX_ROWS:
                raise ValueError("This local edition supports up to 100,000 records per snapshot.")
        return header, rows
    except (UnicodeDecodeError, csv.Error, StopIteration) as exc:
        raise ValueError("Use a valid UTF-8 CSV with a header.") from exc


def profile(header, rows):
    output = {}
    for col in header:
        values = [r[col] for r in rows if r[col] is not None]
        numbers = [numeric(v) for v in values]
        valid_numbers = [v for v in numbers if v is not None]
        leading_codes = any(re.match(r"^[+-]?0\d+", v.strip()) for v in values)
        if values and len(valid_numbers) == len(values) and not leading_codes:
            kind = "integer" if all(Decimal(v) == Decimal(v).to_integral_value() and -(2**63) <= Decimal(v) < 2**63 for v in values) else "number"
        elif values and len(valid_numbers) / len(values) >= .8 and not leading_codes:
            kind = "mixed"
        else:
            kind = "text" if values else "empty"
        counts = Counter(canonical(v) for v in values)
        mean_len = sum(len(v) for v in values) / max(len(values), 1)
        text_candidate = kind == "text" and mean_len >= 50 and sum(" " in v for v in values) / max(len(values), 1) > .5
        ordered = sorted(valid_numbers)
        output[col] = {
            "kind": kind, "missing": len(rows) - len(values),
            "missing_pct": round(100 * (len(rows) - len(values)) / max(len(rows), 1), 3),
            "unique_values": len(counts), "numeric_values": len(valid_numbers),
            "mean": sum(v / len(valid_numbers) for v in valid_numbers) if valid_numbers else None,
            "min": min(valid_numbers) if valid_numbers else None,
            "max": max(valid_numbers) if valid_numbers else None,
            "q1": ordered[int((len(ordered) - 1) * .25)] if ordered else None,
            "q3": ordered[int((len(ordered) - 1) * .75)] if ordered else None,
            "top_values": counts.most_common(6), "semantic_candidate": text_candidate,
            "average_length": round(mean_len, 1),
        }
    return output


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
        config = {"identity": [], "important": [], "semantic": [], "setup_complete": False,
                  "cloud_allowed": False, "growth_threshold": 25, "missing_threshold": 5,
                  "context": "", "embedding_provider": "", "embedding_model": "", "rename_decisions": [], **ANALYSIS_DEFAULTS}
        with self.db() as db:
            db.execute("INSERT INTO datasets VALUES (?,?,?,?)", (identifier, name, now(), dump(config)))
        return self.dataset(identifier)

    def dataset(self, identifier):
        with self.db() as db:
            row = db.execute("SELECT * FROM datasets WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise ValueError("Dataset not found.")
        return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "config": {**ANALYSIS_DEFAULTS, **json.loads(row["config"])}}

    def datasets(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM datasets ORDER BY created_at DESC")]
        return [dict(self.dataset(i), versions=len(self.versions(i))) for i in ids]

    def configure(self, identifier, incoming):
        from .exploration import DATASET_TYPES, ROLES, DATE_FORMATS
        if not isinstance(incoming, dict):
            raise ValueError("Dataset setup must be a JSON object.")
        dataset = self.dataset(identifier)
        config = dataset["config"]
        versions = self.versions(identifier)
        columns = set(c for v in versions for c in v["columns"])
        for key in ("identity", "important", "semantic"):
            value = incoming.get(key, config[key])
            if not isinstance(value, list) or any(not isinstance(c, str) or c not in columns for c in value):
                raise ValueError(f"Choose existing columns for {key}.")
            config[key] = list(dict.fromkeys(value))
        for key in ("cloud_allowed", "setup_complete"):
            if key in incoming:
                if not isinstance(incoming[key], bool):
                    raise ValueError(f"{key} must be true or false.")
                config[key] = incoming[key]
        for key in ("growth_threshold", "missing_threshold"):
            if key in incoming:
                number = float(incoming[key])
                if not math.isfinite(number) or not 0 < number <= 1000:
                    raise ValueError("Thresholds must be between 0 and 1,000.")
                config[key] = number
        config["context"] = str(incoming.get("context", config["context"]))[:3000]
        ep = incoming.get("embedding_provider", config["embedding_provider"])
        if ep not in ("", "ollama", "openai", "gemini"):
            raise ValueError("Text embeddings need an Ollama, OpenAI, or Gemini connection.")
        config["embedding_provider"] = ep
        config["embedding_model"] = str(incoming.get("embedding_model", config["embedding_model"]))[:120]
        decisions = incoming.get("rename_decisions", config.get("rename_decisions", []))
        if not isinstance(decisions, list) or len(decisions) > 100:
            raise ValueError("Use up to 100 field-link decisions.")
        seen = set()
        for decision in decisions:
            if (not isinstance(decision, dict) or decision.get("old") not in columns or decision.get("new") not in columns
                    or decision["old"] == decision["new"] or decision.get("decision") not in ("confirm", "reject")
                    or (decision["old"], decision["new"]) in seen):
                raise ValueError("Choose existing, different columns and confirm or reject each link once.")
            seen.add((decision["old"], decision["new"]))
        config["rename_decisions"] = decisions
        dataset_type = incoming.get("dataset_type", config["dataset_type"])
        if dataset_type not in DATASET_TYPES:
            raise ValueError("Choose a supported dataset type.")
        config["dataset_type"] = dataset_type
        for key, maximum in (("analysis_goal", 2000), ("row_meaning", 1000)):
            value = incoming.get(key, config[key])
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError(f"{key} must be text of at most {maximum} characters.")
            config[key] = value
        for key in ("date_column", "target_column"):
            value = incoming.get(key, config[key])
            if not isinstance(value, str) or (value and value not in columns):
                raise ValueError(f"Choose an existing field for {key}.")
            config[key] = value
        for key, choices in (("date_format", DATE_FORMATS), ("time_grain", {"day", "month"})):
            value = incoming.get(key, config[key])
            if value not in choices:
                raise ValueError(f"Choose a supported {key}.")
            config[key] = value
        roles = incoming.get("column_roles", config["column_roles"])
        orders = incoming.get("category_orders", config["category_orders"])
        if not isinstance(roles, dict) or any(c not in columns or role not in ROLES for c, role in roles.items()):
            raise ValueError("Field meanings must use existing fields and supported roles.")
        if not isinstance(orders, dict):
            raise ValueError("Category orders must be a field-to-labels mapping.")
        for column, labels in orders.items():
            if (column not in columns or not isinstance(labels, list) or not 2 <= len(labels) <= 100
                    or any(not isinstance(label, str) or not label or len(label) > 200 for label in labels) or len(set(labels)) != len(labels)):
                raise ValueError("An ordinal order needs 2–100 different text labels for an existing field.")
        config["column_roles"], config["category_orders"] = roles, orders
        with self.db() as db:
            db.execute("UPDATE datasets SET config=? WHERE id=?", (dump(config), identifier))
        return self.dataset(identifier)

    def versions(self, dataset_id):
        self.dataset(dataset_id)
        with self.db() as db:
            rows = db.execute("SELECT * FROM versions WHERE dataset_id=? ORDER BY ordinal", (dataset_id,)).fetchall()
        return [{"id": r["id"], "ordinal": r["ordinal"], "label": r["label"], "captured_at": r["captured_at"],
                 "digest": r["digest"], "row_count": r["row_count"], "columns": json.loads(r["columns_json"]),
                 "profile": json.loads(r["profile_json"]), "table": r["table_name"]} for r in rows]

    def ingest(self, dataset_id, label, content):
        header, rows = parse_csv(content)
        facts = profile(header, rows)
        label = str(label).strip()[:100]
        if not label:
            raise ValueError("Give this version a label.")
        with WRITE_LOCK, self.db() as db:
            self.dataset(dataset_id)
            total = db.execute("SELECT count(*) FROM versions WHERE dataset_id=?", (dataset_id,)).fetchone()[0]
            if total >= 5:
                raise ValueError("This timeline already has five versions. Start another dataset timeline; saved snapshots are never silently removed.")
            version_id = uid()
            table = "snapshot_" + version_id
            sql_types = {"integer": "INTEGER", "number": "REAL"}
            db.execute(f"CREATE TABLE {quote(table)} ({','.join(quote(c)+' '+sql_types.get(facts[c]['kind'], 'TEXT') for c in header)})")
            def typed(row):
                return [None if row[c] is None else int(Decimal(row[c])) if facts[c]["kind"] == "integer"
                        else float(row[c]) if facts[c]["kind"] == "number" else row[c] for c in header]
            db.executemany(f"INSERT INTO {quote(table)} VALUES ({','.join('?' for _ in header)})", (typed(r) for r in rows))
            db.execute("INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                version_id, dataset_id, total + 1, label, now(), hashlib.sha256(content).hexdigest(), len(rows),
                dump(header), dump(facts), content, table))
        return self.versions(dataset_id)[-1]

    def source(self, dataset_id, version_id):
        with self.db() as db:
            row = db.execute("SELECT source FROM versions WHERE id=? AND dataset_id=?", (version_id, dataset_id)).fetchone()
        if not row:
            raise ValueError("Version not found in this dataset.")
        return bytes(row[0])

    def rows(self, dataset_id, version):
        return parse_csv(self.source(dataset_id, version["id"]))[1]

    def query(self, dataset_id, sql, version_ids=None):
        if not isinstance(sql, str) or len(sql) > 12_000:
            raise ValueError("Queries must be at most 12,000 characters.")
        tables = {v["table"] for v in self.versions(dataset_id) if version_ids is None or v["id"] in version_ids}
        if not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            raise ValueError("Only read-only SELECT queries are available.")
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2
        safe_functions = {"count", "sum", "avg", "min", "max", "total", "round", "abs", "coalesce", "ifnull",
                          "nullif", "cast", "lower", "upper", "length", "substr", "substring", "trim", "ltrim", "rtrim",
                          "replace", "like", "glob", "date", "datetime", "strftime", "julianday", "typeof",
                          "row_number", "rank", "dense_rank", "lag", "lead", "first_value", "last_value"}
        def authorize(action, arg1, arg2, database, trigger):
            if action == sqlite3.SQLITE_READ:
                return sqlite3.SQLITE_OK if arg1 in tables else sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION:
                return sqlite3.SQLITE_OK if (arg2 or "").lower() in safe_functions else sqlite3.SQLITE_DENY
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
            payload = {"columns": names, "rows": rows, "truncated": len(result) > 200, "row_limit": 200}
            if len(dump(payload)) > 500_000:
                raise ValueError("Query results are too wide. Select fewer columns or records.")
            return payload
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise ValueError(f"Query could not run: {exc}") from exc
        finally:
            db.close()

    def save_report(self, dataset_id, report):
        with self.db() as db:
            db.execute("INSERT INTO reports VALUES (?,?,?,?)", (report["id"], dataset_id, report["created_at"], dump(report)))

    def report(self, dataset_id, report_id=None):
        self.dataset(dataset_id)
        with self.db() as db:
            row = db.execute("SELECT payload FROM reports WHERE dataset_id=? " + ("AND id=? " if report_id else "") + "ORDER BY created_at DESC LIMIT 1",
                             (dataset_id, report_id) if report_id else (dataset_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            feedback = db.execute("SELECT rating,note FROM feedback WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            raise ValueError("Run not found.")
        return dict(row, payload=json.loads(row["payload"]), feedback=dict(feedback) if feedback else None)

    def runs(self, dataset_id):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM runs WHERE dataset_id=? ORDER BY started_at DESC LIMIT 50", (dataset_id,))]
        return [self.run(i) for i in ids]

    def history(self, dataset_id, report_id):
        with self.db() as db:
            rows = db.execute("SELECT * FROM messages WHERE dataset_id=? AND report_id=? ORDER BY created_at DESC LIMIT 12",
                              (dataset_id, report_id)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recover_interrupted_runs(self):
        with self.db() as db:
            rows = db.execute("SELECT id,payload FROM runs WHERE status='running'").fetchall()
            for row in rows:
                payload = json.loads(row["payload"])
                payload["error"] = "The server stopped before this run finished. Start a new investigation."
                db.execute("UPDATE runs SET status='failed',finished_at=?,payload=? WHERE id=?", (now(), dump(payload), row["id"]))

    def message(self, dataset_id, report_id, role, content, run_id):
        with self.db() as db:
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (uid(), dataset_id, report_id, role, content, now(), run_id))
