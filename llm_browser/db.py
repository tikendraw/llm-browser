"""SQLite-backed chat history."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Generator

from llm_browser.config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    provider    TEXT    NOT NULL,
    query       TEXT    NOT NULL,
    response    TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    duration_ms INTEGER
);
"""


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _conn() as con:
        con.executescript(_SCHEMA)


def save_chat(
    provider: str,
    query: str,
    response: str,
    duration_ms: int | None = None,
) -> int:
    """Insert a chat record and return its id."""
    with _conn() as con:
        cur = con.execute(
            "INSERT INTO chats (provider, query, response, duration_ms) VALUES (?, ?, ?, ?)",
            (provider, query, response, duration_ms),
        )
        return cur.lastrowid  # type: ignore[return-value]


@dataclass
class Chat:
    id: int
    provider: str
    query: str
    response: str
    created_at: str
    duration_ms: int | None


def get_chats(
    limit: int = 20,
    provider: str | None = None,
) -> list[Chat]:
    """Return recent chats, newest first."""
    with _conn() as con:
        if provider:
            rows = con.execute(
                "SELECT * FROM chats WHERE provider = ? ORDER BY id DESC LIMIT ?",
                (provider, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM chats ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [Chat(**dict(r)) for r in rows]


def get_chat(chat_id: int) -> Chat | None:
    """Fetch a single chat by id."""
    with _conn() as con:
        row = con.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
    return Chat(**dict(row)) if row else None
