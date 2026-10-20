"""Read-side queries shared by the CLI and the web app."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .parse import fold

MAX_RESULTS = 100


@dataclass
class Version:
    sha256: str
    file_date: str
    original_url: str


@dataclass
class ProductSummary:
    product_key: str
    name: str
    form: str
    strength: str
    dci: str
    holder: str
    first_seen: str  # file_date of the first version listing it
    last_seen: str  # file_date of the last version listing it
    versions_listed: int
    in_latest: bool  # listed in the newest stored version
    notice_type: str  # as of last_seen
    notice_date: str | None
    resume_raw: str
    remarks: str


@dataclass
class Period:
    """Consecutive versions in which the product's status did not change."""

    first: str
    last: str
    versions: int
    notice_type: str
    notice_date: str | None
    resume_raw: str
    remarks: str
    source_urls: list[str] = field(default_factory=list)


def versions(conn: sqlite3.Connection) -> list[Version]:
    cur = conn.execute("SELECT sha256, file_date, original_url FROM versions ORDER BY file_date")
    return [Version(*r) for r in cur]


def search(conn: sqlite3.Connection, query: str) -> list[ProductSummary]:
    """Products whose brand name, DCI or holder contains the query
    (case-, diacritic- and whitespace-insensitive)."""
    q = fold(query)
    if len(q) < 3:
        return []
    latest = conn.execute("SELECT max(file_date) FROM versions").fetchone()[0]
    cur = conn.execute(
        """SELECT r.*, v.file_date FROM rows r JOIN versions v USING (sha256)
           WHERE r.search_text LIKE ? OR replace(r.search_text, ' ', '') LIKE ?
           ORDER BY v.file_date""",
        (f"%{q}%", f"%{q.replace(' ', '')}%"),
    )
    by_key: dict[str, list[sqlite3.Row]] = {}
    for r in cur:
        by_key.setdefault(r["product_key"], []).append(r)
    out = []
    for key, rows in by_key.items():
        last = rows[-1]
        out.append(
            ProductSummary(
                product_key=key,
                name=last["name"],
                form=last["form"],
                strength=last["strength"],
                dci=last["dci"],
                holder=last["holder"],
                first_seen=rows[0]["file_date"],
                last_seen=last["file_date"],
                versions_listed=len({r["sha256"] for r in rows}),
                in_latest=last["file_date"] == latest,
                notice_type=last["notice_type"],
                notice_date=last["notice_date"],
                resume_raw=last["resume_raw"],
                remarks=last["remarks"],
            )
        )
    # Stable sorts, least important key first: name, then most recently seen,
    # then products listed in the current file on top.
    out.sort(key=lambda p: p.name)
    out.sort(key=lambda p: p.last_seen, reverse=True)
    out.sort(key=lambda p: not p.in_latest)
    return out[:MAX_RESULTS]


def history(conn: sqlite3.Connection, key: str) -> list[Period]:
    """The product's status over time, one Period per run of unchanged status."""
    cur = conn.execute(
        """SELECT v.file_date, v.original_url, r.notice_type, r.notice_date, r.resume_raw, r.remarks
           FROM rows r JOIN versions v USING (sha256)
           WHERE r.product_key = ? ORDER BY v.file_date, r.notice_date""",
        (key,),
    )
    periods: list[Period] = []
    for r in cur:
        state = (r["notice_type"], r["notice_date"], r["resume_raw"], r["remarks"])
        p = periods[-1] if periods else None
        if p and (p.notice_type, p.notice_date, p.resume_raw, p.remarks) == state:
            if p.last != r["file_date"]:
                p.versions += 1
                p.source_urls.append(r["original_url"])
            p.last = r["file_date"]
        else:
            periods.append(
                Period(r["file_date"], r["file_date"], 1, *state, source_urls=[r["original_url"]])
            )
    return periods
