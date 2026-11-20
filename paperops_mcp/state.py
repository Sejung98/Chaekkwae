"""SQLite log: every search (with the keywords sent to each database) and every fetch attempt
(with the reason it worked or failed).

Keep state_dir on a local disk when possible (SQLite locking is unreliable on NFS/Lustre).
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS queries (
    query_id       TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,          -- search | paper | fulltext | supplement
    created_at     TEXT NOT NULL,
    params_json    TEXT NOT NULL,          -- what was asked (keywords, filters, identifier)
    summary_json   TEXT NOT NULL,          -- what came back (counts, per-source status)
    api_calls_json TEXT NOT NULL,          -- the exact API calls (secrets masked)
    result_path    TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    paper_key   TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- fulltext | supplement
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,             -- ok | fail | skip
    reason      TEXT NOT NULL,             -- see http.REASONS
    detail      TEXT,
    url         TEXT,
    http_status INTEGER,
    files_json  TEXT
);
CREATE INDEX IF NOT EXISTS attempts_paper ON attempts (paper_key);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_query_id(kind: str) -> str:
    return f"{kind[:2]}_{dt.datetime.now(dt.timezone.utc):%Y%m%d%H%M%S}_{secrets.token_hex(3)}"


class StateDB:
    def __init__(self, db_path: Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def save_query(self, kind: str, params: dict, summary: dict, api_calls: list,
                   result_path: str | None = None, query_id: str | None = None) -> str:
        qid = query_id or new_query_id(kind)
        with self._conn() as c:
            c.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?)",
                      (qid, kind, now(), json.dumps(params, ensure_ascii=False),
                       json.dumps(summary, ensure_ascii=False), json.dumps(api_calls, ensure_ascii=False),
                       result_path))
        return qid

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM queries WHERE query_id = ?", (query_id,)).fetchone()
            if row is None:
                return None
            out = _query_row(row, full=True)
            out["attempts"] = [_attempt_row(r) for r in c.execute(
                "SELECT * FROM attempts WHERE query_id = ? ORDER BY attempt_id", (query_id,))]
        return out

    def list_queries(self, kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM queries", []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY created_at DESC, rowid DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [_query_row(r, full=False) for r in rows]

    def add_attempt(self, query_id: str, paper_key: str, kind: str, a: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO attempts (query_id, created_at, paper_key, kind, source, status, reason, detail,"
                " url, http_status, files_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (query_id, now(), paper_key, kind, a["source"], a["status"], a["reason"], a.get("detail"),
                 a.get("url"), a.get("http_status"), json.dumps(a.get("files") or [], ensure_ascii=False)))

    def attempts(self, paper_key: str | None = None, status: str | None = None, reason: str | None = None,
                 since_hours: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where, args = [], []
        for col, val in (("paper_key", paper_key), ("status", status), ("reason", reason)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if since_hours:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(since_hours))
            where.append("created_at >= ?")
            args.append(cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"))
        sql = "SELECT * FROM attempts" + (" WHERE " + " AND ".join(where) if where else "")
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY attempt_id DESC LIMIT ?", (*args, int(limit))).fetchall()
        return [_attempt_row(r) for r in rows]


def _query_row(row: sqlite3.Row, full: bool) -> dict[str, Any]:
    out = {"query_id": row["query_id"], "kind": row["kind"], "created_at": row["created_at"],
           "params": json.loads(row["params_json"]), "summary": json.loads(row["summary_json"]),
           "result_path": row["result_path"]}
    if full:
        out["api_queries"] = json.loads(row["api_calls_json"])
    return out


def _attempt_row(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in ("attempt_id", "query_id", "created_at", "paper_key", "kind", "source",
                               "status", "reason", "detail", "url", "http_status")}
    out["files"] = json.loads(row["files_json"] or "[]")
    return out
