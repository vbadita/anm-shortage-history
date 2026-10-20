"""Find candidate file URLs: Wayback CDX, archived pages, live page, known list."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlencode

import requests

from .config import FILES_BASE, FILES_PREFIX, KNOWN_FILES, PAGE_URL
from .http import PoliteClient
from .logs import log
from .names import canonical_url, is_file_url, parse_file_date

CDX_URL = "https://web.archive.org/cdx/search/cdx"


class DiscoveryError(RuntimeError):
    pass


@dataclass
class Capture:
    timestamp: str
    original: str  # URL exactly as Wayback archived it
    digest: str


@dataclass
class Candidate:
    url: str
    file_date: date | None
    sources: set[str] = field(default_factory=set)
    # one capture per distinct content digest (earliest seen)
    captures: dict[str, Capture] = field(default_factory=dict)

    def add_capture(self, cap: Capture) -> None:
        if cap.digest not in self.captures or cap.timestamp < self.captures[cap.digest].timestamp:
            self.captures[cap.digest] = cap


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.hrefs.extend(v for k, v in attrs if k == "href" and v)


def file_links(html: str, base: str = PAGE_URL) -> set[str]:
    parser = _LinkParser()
    parser.feed(html)
    urls = {canonical_url(h, base) for h in parser.hrefs}
    return {u for u in urls if is_file_url(u) and u.startswith("https://www.anm.ro/")}


def wayback_url(timestamp: str, original: str) -> str:
    # id_ = original bytes, no Wayback toolbar or rewritten links
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


class Discovery:
    def __init__(self, client: PoliteClient) -> None:
        self.client = client

    def _cdx(self, **params: str) -> list[dict[str, str]]:
        query = urlencode({**params, "output": "json"})
        resp = self.client.get(f"{CDX_URL}?{query}", source="cdx")
        if resp.status_code != 200:
            raise DiscoveryError(f"CDX HTTP {resp.status_code}")
        rows = resp.json() if resp.text.strip() else []
        if not rows:
            return []
        header, *body = rows
        return [dict(zip(header, r, strict=True)) for r in body]

    def cdx_file_captures(self) -> list[Capture]:
        """Every 200-capture under the files folder. collapse=digest drops
        consecutive identical captures of the same URL (rows are sorted by
        URL, then time), so each row is a distinct (url, content) pair."""
        rows = self._cdx(
            url=FILES_PREFIX,
            matchType="prefix",
            collapse="digest",
            filter="statuscode:200",
            fl="timestamp,original,digest",
        )
        caps = [Capture(r["timestamp"], r["original"], r["digest"]) for r in rows]
        log("cdx_prefix", phase="discover", url=FILES_PREFIX, result="ok", captures=len(caps))
        return caps

    def cdx_url_captures(self, url: str) -> list[Capture]:
        rows = self._cdx(
            url=url, collapse="digest", filter="statuscode:200", fl="timestamp,original,digest"
        )
        return [Capture(r["timestamp"], r["original"], r["digest"]) for r in rows]

    def page_versions(self) -> list[Capture]:
        rows = self._cdx(
            url=PAGE_URL.removeprefix("https://"),
            collapse="digest",
            filter="statuscode:200",
            fl="timestamp,original,digest",
        )
        caps = [Capture(r["timestamp"], r["original"], r["digest"]) for r in rows]
        log("cdx_page", phase="discover", url=PAGE_URL, result="ok", versions=len(caps))
        return caps

    def archived_page_links(self, cap: Capture) -> set[str]:
        url = wayback_url(cap.timestamp, cap.original)
        try:
            resp = self.client.get(url, source="wayback")
        except requests.RequestException as exc:
            log(
                "page_fetch",
                logging.WARNING,
                phase="discover",
                url=url,
                result="error",
                error=str(exc),
            )
            return set()
        if resp.status_code != 200:
            log(
                "page_fetch",
                logging.WARNING,
                phase="discover",
                url=url,
                result=f"http_{resp.status_code}",
            )
            return set()
        links = file_links(resp.text)
        log("page_fetch", phase="discover", url=url, result="ok", links=sorted(links))
        return links

    def live_page_links(self) -> set[str]:
        resp = self.client.get(PAGE_URL, source="live")
        if resp.status_code != 200:
            raise DiscoveryError(f"live page HTTP {resp.status_code}")
        links = file_links(resp.text)
        log("page_fetch", phase="discover", url=PAGE_URL, result="ok", links=sorted(links))
        return links


def merge(
    candidates: dict[str, Candidate], url: str, source: str, capture: Capture | None = None
) -> None:
    cand = candidates.get(url)
    if cand is None:
        cand = candidates[url] = Candidate(url=url, file_date=parse_file_date(url))
    cand.sources.add(source)
    if capture:
        cand.add_capture(capture)


def discover_all(disc: Discovery) -> dict[str, Candidate]:
    """Backfill discovery. CDX failures are fatal (no history without them);
    a single archived page failing is not."""
    cands: dict[str, Candidate] = {}

    for cap in disc.cdx_file_captures():
        url = canonical_url(cap.original)
        if is_file_url(url):  # drops crawler junk like '...pdfPrin'
            merge(cands, url, "wayback", cap)

    for page in disc.page_versions():
        for url in disc.archived_page_links(page):
            merge(cands, url, "page")

    try:
        for url in disc.live_page_links():
            merge(cands, url, "live_page")
    except (DiscoveryError, requests.RequestException) as exc:
        log(
            "live_page",
            logging.WARNING,
            phase="discover",
            url=PAGE_URL,
            result="error",
            error=str(exc),
        )

    for name in KNOWN_FILES:
        merge(cands, canonical_url(FILES_BASE + name), "known")

    log("candidates", phase="discover", result="ok", count=len(cands))
    return cands
