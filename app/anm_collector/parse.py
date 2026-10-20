"""PDF -> rows for the ANM discontinuity table.

Every version (2017-2026) is one 11-column table drawn with cell rectangles, so
pdfplumber's line-based table detection gives exact cells. Known quirks, all
found by surveying the 68 stored versions:
  * some versions draw an extra, always-empty 12th column (a border line)
  * dates are dd.mm.yyyy in most versions, US m/d/yyyy in a few
  * long text overflows into the next cell ("M 15.05.2026")
  * row order and "Nr crt" numbering change between versions
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pdfplumber

# Bump when parsing logic changes: stored versions parsed with an older value are re-parsed.
PARSER_VERSION = 2  # 2: header split over two table rows (2024-10-14) was stored as data

COLUMNS = (
    "nr",
    "name",
    "form",
    "strength",
    "holder",
    "country",
    "dci",
    "notice_date_raw",
    "notice_type",
    "resume_raw",
    "remarks",
)
# First letters of each header cell, lower-case, diacritics removed.
EXPECTED_HEADER = (
    "nr",
    "denumire",
    "forma",
    "concentr",
    "firma",
    "tara",
    "dci",
    "data adresa",
    "tip notif",
    "data estim",
    "observ",
)

_DMY = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)")
_MDY = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")


class LayoutChanged(ValueError):
    """The table no longer has the columns this parser knows. Never guess."""


@dataclass
class Row:
    page: int
    nr: str
    name: str
    form: str
    strength: str
    holder: str
    country: str
    dci: str
    notice_date: date | None
    notice_date_raw: str
    notice_type: str
    resume_raw: str
    remarks: str
    product_key: str


@dataclass
class ParseResult:
    rows: list[Row] = field(default_factory=list)
    pages: int = 0
    tables: int = 0
    header_rows: int = 0
    dropped_empty_columns: int = 0
    unparsed_dates: int = 0


def fold(text: str) -> str:
    """Upper-case, strip diacritics (ROMÂNIA -> ROMANIA), collapse whitespace.
    The micro sign becomes 'u' ("20 µg" -> "20 UG"): NFKD turns it into Greek mu,
    which upper-cases to U+039C (Greek capital mu): looks like M, never matches M or U."""
    text = text.replace("\u00b5", "u").replace("\u03bc", "u")  # micro sign, Greek mu
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_ish = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", ascii_ish).strip().upper()


def product_key(name: str, form: str, strength: str) -> str:
    """Same product across versions despite spacing/diacritics differences:
    'CERTICAN 0,75 mg' and 'CERTICAN 0,75mg' -> same key."""
    return "|".join(fold(part).replace(" ", "") for part in (name, form, strength))


def parse_notice_date(raw: str) -> date | None:
    """Find a date inside the cell (overflow text from the neighbour cell may
    surround it). 'dd.mm.yyyy' is Romanian day-first; 'm/d/yyyy' is US
    month-first (confirmed: the same row reads 12.01.2017 in one version
    and 1/12/2017 in the next)."""
    for pattern, order in ((_DMY, "dmy"), (_MDY, "mdy")):
        for a, b, y in pattern.findall(raw):
            day, month = (int(a), int(b)) if order == "dmy" else (int(b), int(a))
            try:
                return date(int(y), month, day)
            except ValueError:
                continue
    return None


def _clean(cell: str | None) -> str:
    return re.sub(r"\s+", " ", (cell or "")).strip()


def normalize_table(table: list[list[str | None]]) -> tuple[list[list[str]], int]:
    """Clean cells and drop trailing columns that are empty in every row."""
    rows = [[_clean(c) for c in r] for r in table]
    dropped = 0
    while (
        rows
        and len(rows[0]) > len(COLUMNS)
        and all(len(r) == len(rows[0]) for r in rows)
        and not any(r[-1] for r in rows)
    ):
        rows = [r[:-1] for r in rows]
        dropped += 1
    return rows, dropped


def is_header(cells: list[str]) -> bool:
    # Some pages split the header over two table rows, so no single cell is always present.
    return (
        cells[0].lower().startswith("nr")
        or cells[7].lower() == "data adresa"
        or (fold(cells[1]).startswith("DENUMIRE") and fold(cells[6]) == "DCI")
    )


def check_header(cells: list[str]) -> None:
    folded = [fold(c).lower() for c in cells]
    matches = sum(
        1
        for got, want in zip(folded, EXPECTED_HEADER, strict=True)
        if not got or got.startswith(want)
    )  # empty header cells occur; tolerate
    if matches < 9:
        raise LayoutChanged(f"unexpected header: {cells}")


def rows_from_table(cells_rows: list[list[str]], page_no: int, result: ParseResult) -> None:
    for cells in cells_rows:
        if len(cells) != len(COLUMNS):
            raise LayoutChanged(f"page {page_no}: row with {len(cells)} cells: {cells[:3]}")
        if is_header(cells):
            check_header(cells)
            result.header_rows += 1
            continue
        rec = dict(zip(COLUMNS, cells, strict=True))
        if not (rec["name"] or rec["dci"]):
            continue  # spacer / empty row
        notice_date = parse_notice_date(rec["notice_date_raw"])
        if notice_date is None and rec["notice_date_raw"]:
            result.unparsed_dates += 1
        result.rows.append(
            Row(
                page=page_no,
                nr=rec["nr"].rstrip("."),
                name=rec["name"],
                form=rec["form"],
                strength=rec["strength"],
                holder=rec["holder"],
                country=rec["country"],
                dci=rec["dci"],
                notice_date=notice_date,
                notice_date_raw=rec["notice_date_raw"],
                notice_type=rec["notice_type"],
                resume_raw=rec["resume_raw"],
                remarks=rec["remarks"],
                product_key=product_key(rec["name"], rec["form"], rec["strength"]),
            )
        )


def parse_pdf(path: Path) -> ParseResult:
    result = ParseResult()
    with pdfplumber.open(path) as pdf:
        result.pages = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables():
                cells_rows, dropped = normalize_table(table)
                result.tables += 1
                result.dropped_empty_columns += dropped
                rows_from_table(cells_rows, page_no, result)
    if not result.rows:
        raise LayoutChanged(f"{path.name}: no rows found")
    return result
