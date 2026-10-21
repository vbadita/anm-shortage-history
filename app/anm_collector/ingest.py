"""Parse stored PDF versions that are not yet in the SQLite store."""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .db import parsed_current, store_version
from .logs import log
from .parse import PARSER_VERSION, LayoutChanged, parse_pdf
from .storage import ManifestRow


@dataclass
class ParseRun:
    parsed: list[str] = field(default_factory=list)  # sha256
    failed: list[str] = field(default_factory=list)
    pending_after: int = 0  # stored versions still not parsed with PARSER_VERSION


def pending_versions(rows: Iterable[ManifestRow], conn: sqlite3.Connection) -> list[ManifestRow]:
    done = parsed_current(conn)
    todo = {r.sha256: r for r in rows if r.status == "new" and r.sha256 not in done}
    return sorted(todo.values(), key=lambda r: r.file_date)


def parse_pending(
    data_dir: Path, rows: list[ManifestRow], conn: sqlite3.Connection, only: set[str] | None = None
) -> ParseRun:
    """Parse pending versions (all, or only the given sha256s). A version whose
    layout is not recognised fails loudly and is left unparsed; others continue."""
    run = ParseRun()
    for row in pending_versions(rows, conn):
        if only is not None and row.sha256 not in only:
            continue
        started = time.monotonic()
        try:
            result = parse_pdf(data_dir / row.path)
        except Exception as exc:  # LayoutChanged, or a malformed PDF (pdfminer's own errors)
            log(
                "parse",
                logging.ERROR,
                phase="parse",
                url=row.original_url,
                result="layout_changed" if isinstance(exc, LayoutChanged) else "error",
                file_date=row.file_date,
                error=repr(exc)[:300],
            )
            run.failed.append(row.sha256)
            continue
        store_version(
            conn,
            sha256=row.sha256,
            file_date=row.file_date,
            path=row.path,
            original_url=row.original_url,
            result=result,
        )
        run.parsed.append(row.sha256)
        log(
            "parse",
            phase="parse",
            url=row.original_url,
            result="ok",
            file_date=row.file_date,
            rows=len(result.rows),
            pages=result.pages,
            unparsed_dates=result.unparsed_dates,
            parser_version=PARSER_VERSION,
            duration_s=round(time.monotonic() - started, 1),
        )
    run.pending_after = len(pending_versions(rows, conn))
    return run
