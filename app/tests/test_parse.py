from datetime import date
from pathlib import Path

import pytest

from anm_collector.parse import (
    LayoutChanged,
    ParseResult,
    check_header,
    fold,
    normalize_table,
    parse_notice_date,
    parse_pdf,
    product_key,
    rows_from_table,
)

FIXTURES = Path(__file__).parent / "fixtures"
HEADER = [
    "Nr crt",
    "Denumire comerciala",
    "Forma farmaceutica",
    "Concentratie",
    "Firma Detinatoare",
    "Tara Detinatoare",
    "DCI",
    "Data adresa",
    "Tip Notificare",
    "Data estimativa de reluare a comercializarii",
    "Observatii",
]


# ---- dates -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07.09.2016", date(2016, 9, 7)),  # Romanian day-first
        ("6/20/2017", date(2017, 6, 20)),  # US month-first (2021-11-19 version)
        ("1/12/2017", date(2017, 1, 12)),  # ambiguous by shape: US order -> 12 Jan
        ("M 15.05.2026", date(2026, 5, 15)),  # overflow letter from the neighbour cell
        ("ARA1/30/2019", date(2019, 1, 30)),
        ("09.08.2022 1", date(2022, 8, 9)),
    ],
)
def test_parse_notice_date(raw, expected):
    assert parse_notice_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "SO0N7U.1M1).", "2043..0045..", "31.02.2020", "Data adresa"])
def test_parse_notice_date_unreadable(raw):
    assert parse_notice_date(raw) is None


# ---- normalisation -----------------------------------------------------------
def test_fold_diacritics_and_micro_sign():
    assert fold("  România  ") == "ROMANIA"
    assert fold("20 µg/ml") == "20 UG/ML"  # not Greek capital mu
    assert fold("20 μg/ml") == "20 UG/ML"


def test_product_key_ignores_spacing_and_case():
    assert product_key("CERTICAN 0,75 mg", "COMPR.", "0,75mg") == product_key(
        "Certican 0,75mg", "COMPR. ", "0,75 mg"
    )


def test_normalize_table_drops_empty_trailing_column():
    table = [[*HEADER, None], ["1", "X"] + [""] * 10]
    rows, dropped = normalize_table(table)
    assert dropped == 1 and all(len(r) == 11 for r in rows)


def test_normalize_table_keeps_non_empty_12th_column():
    rows, dropped = normalize_table([["a"] * 12])
    assert dropped == 0 and len(rows[0]) == 12  # rows_from_table will reject it


# ---- layout guard ------------------------------------------------------------
def test_reordered_header_is_rejected():
    swapped = HEADER.copy()
    swapped[6], swapped[4] = swapped[4], swapped[6]  # DCI <-> holder
    swapped[1], swapped[2] = swapped[2], swapped[1]
    with pytest.raises(LayoutChanged):
        check_header(swapped)


def test_wrong_cell_count_is_rejected():
    with pytest.raises(LayoutChanged):
        rows_from_table([["1", "X"] + [""] * 8], page_no=1, result=ParseResult())


# ---- real pages, one per known quirk -----------------------------------------
def test_real_page_2026():
    r = parse_pdf(FIXTURES / "anm-2026-10-09-p1.pdf")
    assert len(r.rows) == 36 and r.unparsed_dates == 0
    first = r.rows[0]
    assert (
        first.nr,
        first.name,
        first.dci,
        first.notice_date,
        first.notice_type,
        first.remarks,
    ) == (
        "1",
        "CERTICAN 0,75 mg",
        "EVEROLIMUS",
        date(2016, 9, 7),
        "Discontinuitate permanenta",
        "Motive comerciale",
    )


def test_real_page_us_dates_2021():
    r = parse_pdf(FIXTURES / "anm-2021-11-19-p1-mdy.pdf")
    by_name = {row.name: row for row in r.rows}
    assert by_name["URSOSAN"].notice_date == date(2017, 6, 20)  # printed 6/20/2017
    assert by_name["URSOSAN"].notice_date_raw == "6/20/2017"


def test_real_page_with_empty_12th_column_2024():
    r = parse_pdf(FIXTURES / "anm-2024-04-12-p1-12cols.pdf")
    assert r.dropped_empty_columns == 1 and len(r.rows) == 33
    assert r.rows[0].name == "CLOPIXOL 25 mg" and r.rows[0].dci == "ZUCLOPENTHIXOLUM"
