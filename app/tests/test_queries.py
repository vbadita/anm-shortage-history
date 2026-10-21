from datetime import date

import pytest

from anm_collector import db, queries
from anm_collector.parse import ParseResult, Row, product_key


def row(name, notice_type, resume=""):
    return Row(
        page=1,
        nr="1",
        name=name,
        form="COMPR.",
        strength="10mg",
        holder="H",
        country="RO",
        dci="SUBSTANTIA",
        notice_date=date(2024, 1, 1),
        notice_date_raw="01.01.2024",
        notice_type=notice_type,
        resume_raw=resume,
        remarks="Motive comerciale",
        product_key=product_key(name, "COMPR.", "10mg"),
    )


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.sqlite")
    versions = {  # file_date -> rows in that version
        "2024-01-01": [row("ALFA 10 mg", "Discontinuitate temporara", "mar.24")],
        "2024-02-01": [row("ALFA 10 mg", "Discontinuitate temporara", "mar.24")],
        "2024-03-01": [row("ALFA 10 mg", "Prelungire discontinuitate temporara", "mai.24")],
        "2024-04-01": [row("BETA 10 mg", "Discontinuitate permanenta")],  # ALFA absent
        "2024-05-01": [row("ALFA 10 mg", "Discontinuitate temporara", "iul.24")],
        "2024-06-01": [row("BETA 10 mg", "Discontinuitate permanenta")],  # ALFA absent (latest)
    }
    for i, (d, rows) in enumerate(versions.items()):
        db.store_version(
            c,
            sha256=f"{i:064x}",
            file_date=d,
            path=f"raw/{d}.pdf",
            original_url=f"https://x/{d}.pdf",
            result=ParseResult(rows=rows),
        )
    return c


def test_history_collapses_runs_and_shows_gaps(conn):
    key = product_key("ALFA 10 mg", "COMPR.", "10mg")
    got = [
        (p.first, p.last, p.versions, p.listed, p.notice_type, p.resume_raw)
        for p in queries.history(conn, key)
    ]
    assert got == [
        ("2024-01-01", "2024-02-01", 2, True, "Discontinuitate temporara", "mar.24"),
        ("2024-03-01", "2024-03-01", 1, True, "Prelungire discontinuitate temporara", "mai.24"),
        ("2024-04-01", "2024-04-01", 1, False, "", ""),
        ("2024-05-01", "2024-05-01", 1, True, "Discontinuitate temporara", "iul.24"),
        ("2024-06-01", "2024-06-01", 1, False, "", ""),  # not in the newest file
    ]


def test_search_matches_brand_and_dci_and_flags_current(conn):
    by_name = {p.name: p for p in queries.search(conn, "substantia")}  # DCI, lower-case
    assert set(by_name) == {"ALFA 10 mg", "BETA 10 mg"}
    assert by_name["BETA 10 mg"].in_latest is True
    assert (
        by_name["ALFA 10 mg"].in_latest is False and by_name["ALFA 10 mg"].last_seen == "2024-05-01"
    )
    assert queries.search(conn, "alfa10")[0].name == "ALFA 10 mg"  # space-insensitive
    assert queries.search(conn, "al") == []  # too short: no full scan


def test_suggest_and_tolerant_search(conn):
    assert queries.suggest(conn, "alffa")[0][0] == "ALFA"
    outcome = queries.search_tolerant(conn, "substantai")  # transposed letters in the DCI
    assert outcome.corrected_to == "SUBSTANTIA" and {p.name for p in outcome.results} == {
        "ALFA 10 mg",
        "BETA 10 mg",
    }
    assert queries.search_tolerant(conn, "alfa").corrected_to is None  # exact hit: no correction
    assert queries.search_tolerant(conn, "qqqqqq").results == []
