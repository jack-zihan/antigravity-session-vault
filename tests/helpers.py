"""Shared isolated fixture helpers for unit and integration tests."""

from __future__ import annotations

import datetime
from pathlib import Path
import sqlite3


def iso_ago(days: float = 0.0, minutes: float = 0.0) -> str:
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days, minutes=minutes)
    return dt.isoformat().replace("+00:00", "Z")


def make_summary_db(
    path: Path,
    rows: list[tuple],
    parents: list[tuple[str, str]] | None = None,
    *,
    wal: bool = False,
) -> None:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            last_modified_time TEXT,
            workspace_uris TEXT,
            status TEXT,
            source TEXT,
            project_id TEXT,
            agent_name TEXT,
            parent_conversation_id TEXT,
            nesting_depth INTEGER,
            not_fully_idle INTEGER,
            killed INTEGER,
            step_count INTEGER,
            raw_summary BLOB
        )"""
    )
    conn.execute(
        "CREATE VIRTUAL TABLE conversations_fts USING fts5(conversation_id UNINDEXED, title, workspace_uris)"
    )
    conn.execute("CREATE TABLE conversation_parents (conversation_id TEXT, parent_id TEXT)")
    conn.executemany(
        "INSERT INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    for r in rows:
        conn.execute("INSERT INTO conversations_fts VALUES (?,?,?)", (r[0], r[1], r[3]))
    for cid, pid in parents or ():
        conn.execute("INSERT INTO conversation_parents VALUES (?,?)", (cid, pid))
    conn.commit()
    conn.close()
