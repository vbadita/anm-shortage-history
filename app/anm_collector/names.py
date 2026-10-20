"""Pure helpers: canonical URLs, file-name dates, file extensions."""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

FILE_EXTENSIONS = (".pdf", ".xlsx", ".xls")

# D.M.YYYY anywhere in the name; tolerates a doubled dot ("15..11.2019").
_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\.{1,2}(\d{1,2})\.{1,2}(\d{4})(?!\d)")


def canonical_url(href: str, base: str = "https://www.anm.ro/") -> str:
    """One spelling per file: absolute, https, www.anm.ro, path fully
    percent-encoded exactly once, no query or fragment.

    The page links the same file as 'A B.pdf', 'A%20B.pdf' or mixed;
    unquote-then-quote maps all of them to 'A%20B.pdf'.
    """
    parts = urlsplit(urljoin(base, href.strip()))
    host = (parts.hostname or "").lower()
    if host == "anm.ro":
        host = "www.anm.ro"
    path = quote(unquote(parts.path), safe="/")
    return urlunsplit(("https", host, path, "", ""))


def file_name(url: str) -> str:
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def is_file_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(FILE_EXTENSIONS)


def parse_file_date(name: str) -> date | None:
    """Publication date from a file name such as
    '09.10.2026 Notificari Sunset ... 2016_.pdf'. The first valid D.M.YYYY
    wins; '... IUNIE 2016' has no day/month so it never matches."""
    for d, m, y in _DATE_RE.findall(file_name(name) if "/" in name else name):
        try:
            return date(int(y), int(m), int(d))
        except ValueError:
            continue
    return None
