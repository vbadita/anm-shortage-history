"""SQLite store for parsed versions. One file, read-only for the web app.

Rollback-journal mode (not WAL): the web pod mounts the volume read-only, and a
WAL reader must write the -shm file. Each version is one ~1 s transaction a day;
readers wait for it (busy timeout) and never see half a file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .parse import PARSER_VERSION, ParseResult, fold

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    sha256          TEXT PRIMARY KEY,
    file_date       TEXT NOT NULL,      -- publication date from the file name (ISO)
    path            TEXT NOT NULL,      -- relative to the data dir
    original_url    TEXT NOT NULL,      -- the file on anm.ro
    rows            INTEGER NOT NULL,
    pages           INTEGER NOT NULL,
    unparsed_dates  INTEGER NOT NULL,
    parser_version  INTEGER NOT NULL,
    parsed_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rows (
    sha256          TEXT NOT NULL REFERENCES versions(sha256) ON DELETE CASCADE,
    page            INTEGER NOT NULL,
    nr              TEXT NOT NULL,
    name            TEXT NOT NULL,
    form            TEXT NOT NULL,
    strength        TEXT NOT NULL,
    holder          TEXT NOT NULL,
    country         TEXT NOT NULL,
    dci             TEXT NOT NULL,
    notice_date     TEXT,               -- ISO date, NULL if unreadable
    notice_date_raw TEXT NOT NULL,
    notice_type     TEXT NOT NULL,
    resume_raw      TEXT NOT NULL,      -- "Data estimativa de reluare", as printed
    remarks         TEXT NOT NULL,
    product_key     TEXT NOT NULL,
    search_text     TEXT NOT NULL       -- folded name + DCI + holder, for LIKE search
);
CREATE INDEX IF NOT EXISTS rows_product ON rows(product_key);
CREATE INDEX IF NOT EXISTS rows_sha ON rows(sha256);
"""


BUSY_TIMEOUT_S = 10


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_S, check_same_thread=False
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S)
        conn.execute("PRAGMA journal_mode=DELETE")  # persistent; converts an old WAL database
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def parsed_current(conn: sqlite3.Connection) -> set[str]:
    """sha256 of versions parsed with the current PARSER_VERSION."""
    cur = conn.execute("SELECT sha256 FROM versions WHERE parser_version = ?", (PARSER_VERSION,))
    return {r[0] for r in cur}


def store_version(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    file_date: str,
    path: str,
    original_url: str,
    result: ParseResult,
) -> None:
    with conn:  # one transaction: all rows of a version, or none
        conn.execute("DELETE FROM versions WHERE sha256 = ?", (sha256,))  # cascades to rows
        conn.execute(
            "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sha256,
                file_date,
                path,
                original_url,
                len(result.rows),
                result.pages,
                result.unparsed_dates,
                PARSER_VERSION,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        conn.executemany(
            "INSERT INTO rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    sha256,
                    r.page,
                    r.nr,
                    r.name,
                    r.form,
                    r.strength,
                    r.holder,
                    r.country,
                    r.dci,
                    r.notice_date.isoformat() if r.notice_date else None,
                    r.notice_date_raw,
                    r.notice_type,
                    r.resume_raw,
                    r.remarks,
                    r.product_key,
                    fold(f"{r.name} {r.dci} {r.holder}"),
                )
                for r in result.rows
            ],
        )
