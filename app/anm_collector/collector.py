"""Backfill and sync runs, plus the history report."""

from __future__ import annotations

import csv
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

import requests

from .discovery import (
    Candidate,
    Discovery,
    DiscoveryError,
    discover_all,
    merge,
    wayback_url,
)
from .http import PoliteClient
from .logs import log
from .storage import Catalog, ManifestRow


@dataclass
class RunResult:
    outcomes: Counter[str] = field(default_factory=Counter)  # source:result -> n
    not_found: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # URL with no good copy at all
    new_versions: int = 0
    new_sha256: list[str] = field(default_factory=list)  # versions stored this run

    @property
    def ok(self) -> bool:
        return not self.failed


class Collector:
    def __init__(self, client: PoliteClient, catalog: Catalog) -> None:
        self.client = client
        self.catalog = catalog
        self.discovery = Discovery(client)

    # ---- one download attempt -------------------------------------------
    def _fetch(self, cand: Candidate, source: str, url: str, ts: str = "") -> ManifestRow | None:
        """Returns the manifest row, or None for a plain 404 (nothing to record)."""
        try:
            resp = self.client.get(url, source=source)
        except requests.RequestException as exc:
            log(
                "download",
                logging.ERROR,
                phase="download",
                url=url,
                source=source,
                result="failed",
                error=str(exc),
            )
            return self.catalog.record_failure(
                url=cand.url, file_date=cand.file_date, source=source, wayback_timestamp=ts
            )
        if resp.status_code in (404, 410):
            log("download", phase="download", url=url, source=source, result="not_found")
            return None
        if resp.status_code != 200:
            log(
                "download",
                logging.ERROR,
                phase="download",
                url=url,
                source=source,
                result="failed",
                status=resp.status_code,
            )
            return self.catalog.record_failure(
                url=cand.url, file_date=cand.file_date, source=source, wayback_timestamp=ts
            )
        row = self.catalog.ingest(
            resp.content,
            url=cand.url,
            file_date=cand.file_date,
            source=source,
            wayback_timestamp=ts,
        )
        level = logging.WARNING if row.status == "invalid" else logging.INFO
        log(
            "download",
            level,
            phase="download",
            url=url,
            source=source,
            result=row.status,
            sha256=row.sha256[:12],
            size_bytes=row.size_bytes,
            content_type=row.content_type or resp.headers.get("Content-Type"),
        )
        return row

    # ---- one candidate ----------------------------------------------------
    def process(self, cand: Candidate, result: RunResult, use_cdx_lookup: bool = True) -> None:
        rows: list[ManifestRow] = []
        known = self.catalog.manifest.known_digests()

        if not self.catalog.manifest.has_ok_url(cand.url):
            row = self._fetch(cand, "live", cand.url)
            if row:
                rows.append(row)
                known.add(row.wayback_digest)

        if not cand.captures and use_cdx_lookup and not self.catalog.manifest.has_ok_url(cand.url):
            # Found only via a page link or the known list: ask CDX directly.
            try:
                for cap in self.discovery.cdx_url_captures(cand.url):
                    cand.add_capture(cap)
            except (DiscoveryError, requests.RequestException) as exc:
                log(
                    "cdx_lookup",
                    logging.WARNING,
                    phase="download",
                    url=cand.url,
                    result="error",
                    error=str(exc),
                )

        for digest, cap in sorted(cand.captures.items(), key=lambda kv: kv[1].timestamp):
            if digest in known:
                continue  # same bytes already stored: no request needed
            row = self._fetch(
                cand, "wayback", wayback_url(cap.timestamp, cap.original), cap.timestamp
            )
            if row:
                rows.append(row)
                known.add(row.wayback_digest)

        for r in rows:
            result.outcomes[f"{r.source}:{r.status}"] += 1
            result.new_versions += r.status == "new"
            if r.status == "new":
                result.new_sha256.append(r.sha256)
        if self.catalog.manifest.has_ok_url(cand.url) or any(r.ok for r in rows):
            return
        if rows:  # attempts happened, none produced a valid file
            result.failed.append(cand.url)
        else:
            result.not_found.append(cand.url)

    # ---- modes ------------------------------------------------------------
    def backfill(self) -> tuple[RunResult, dict[str, Candidate]]:
        cands = discover_all(self.discovery)
        result = RunResult()
        ordered = sorted(cands.values(), key=lambda c: (c.file_date or date.min, c.url))
        for i, cand in enumerate(ordered, 1):
            log(
                "candidate",
                phase="download",
                url=cand.url,
                file_date=cand.file_date,
                sources=sorted(cand.sources),
                captures=len(cand.captures),
                progress=f"{i}/{len(ordered)}",
            )
            self.process(cand, result)
        return result, cands

    def sync(self) -> tuple[RunResult, dict[str, Candidate]]:
        """Daily: read the live page; download only links we do not have yet."""
        links = self.discovery.live_page_links()  # raises -> run fails
        if not links:
            raise DiscoveryError("live page has no file link (did the page change?)")
        cands: dict[str, Candidate] = {}
        for url in links:
            merge(cands, url, "live_page")
        result = RunResult()
        for cand in cands.values():
            if self.catalog.manifest.has_ok_url(cand.url):
                log("candidate", phase="sync", url=cand.url, result="unchanged")
                continue
            self.process(cand, result)
        return result, cands


def write_candidates(path, cands: dict[str, Candidate]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["file_date", "url", "sources", "wayback_captures"])
        for c in sorted(cands.values(), key=lambda c: (c.file_date or date.min, c.url)):
            w.writerow(
                [
                    c.file_date or "",
                    c.url,
                    "+".join(sorted(c.sources)),
                    " ".join(sorted(cap.timestamp for cap in c.captures.values())),
                ]
            )


def build_report(rows: list[ManifestRow], result: RunResult | None = None) -> dict:
    stored = [r for r in rows if r.status == "new"]
    dates = sorted({date.fromisoformat(r.file_date) for r in stored if r.file_date})
    gaps = sorted(((b - a).days, a.isoformat(), b.isoformat()) for a, b in pairwise(dates))[::-1][
        :5
    ]
    ok_urls = {r.original_url for r in rows if r.ok}
    report = {
        "unique_versions": len(stored),
        "distinct_dates": len(dates),
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()] if dates else None,
        "biggest_gaps_days": [{"days": d, "from": a, "to": b} for d, a, b in gaps],
        "by_source": dict(Counter(r.source for r in stored)),
        "manifest_rows": dict(Counter(r.status for r in rows)),
        "urls_with_file": len(ok_urls),
        "bytes_stored": sum(r.size_bytes for r in stored),
    }
    if result is not None:
        report["this_run"] = {
            "outcomes": dict(result.outcomes),
            "new_versions": result.new_versions,
            "failed": result.failed,
            "not_found": result.not_found,
        }
    return report
