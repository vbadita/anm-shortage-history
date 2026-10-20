from datetime import date

import pytest

from anm_collector.discovery import file_links
from anm_collector.names import canonical_url, is_file_url, parse_file_date

BASE = "https://www.anm.ro/_/DISCONTINUITATE%20MEDICAMENTE/"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # every naming pattern seen in the wild
        (
            "09.10.2026 Notificari Sunset pt.postat incepand cu luna IUNIE 2016_.pdf",
            date(2026, 10, 9),
        ),
        ("30.07.2025_Notificari discontinuitate de postat pe site .pdf", date(2025, 7, 30)),
        ("Notificari discontinuitate 4.11.2024.pdf", date(2024, 11, 4)),
        (
            "15..11.2019 Notificari Sunset pt.postat incepand cu luna IUNIE 2016.pdf",
            date(2019, 11, 15),
        ),
        (
            "10.07.2017 Notificari Sunset pt.postat incepand cu luna IUNIE 2016 (3).pdf",
            date(2017, 7, 10),
        ),
        # URL-encoded full URL
        (BASE + "02.02.2023%20Notificari%20Sunset.pdf", date(2023, 2, 2)),
    ],
)
def test_parse_file_date(name, expected):
    assert parse_file_date(name) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Notificari Sunset pt.postat incepand cu luna IUNIE 2016.pdf",  # year only
        "32.13.2020 Notificari.pdf",  # impossible day/month
        "report.pdf",
    ],
)
def test_parse_file_date_none(name):
    assert parse_file_date(name) is None


def test_parse_file_date_skips_invalid_first_match():
    assert parse_file_date("99.99.2020 then 01.02.2021.pdf") == date(2021, 2, 1)


def test_canonical_url_equivalent_spellings():
    spellings = [
        "/_/DISCONTINUITATE MEDICAMENTE/10.07.2017 Notificari (3).pdf",
        "/_/DISCONTINUITATE%20MEDICAMENTE/10.07.2017 Notificari (3).pdf",
        "http://anm.ro/_/DISCONTINUITATE%20MEDICAMENTE/10.07.2017%20Notificari%20%283%29.pdf",
        "https://www.anm.ro/_/DISCONTINUITATE MEDICAMENTE/10.07.2017 Notificari (3).pdf#x",
    ]
    assert len({canonical_url(s) for s in spellings}) == 1


def test_is_file_url_rejects_crawler_junk():
    assert is_file_url(BASE + "a.pdf")
    assert is_file_url(BASE + "a.XLSX")
    assert not is_file_url(BASE + "a.pdfPrin")
    assert not is_file_url(BASE + "30.10.2020%20Notificari%20Sunset%20pt.postat%20incepa")


def test_file_links_from_page_html():
    html = """
      <a href="/_/DISCONTINUITATE MEDICAMENTE/04.07.2025 Notificari_.pdf">doc</a>
      <a href="/some/page/">not a file</a>
      <a href="https://example.com/x.pdf">other host</a>
    """
    assert file_links(html) == {
        "https://www.anm.ro/_/DISCONTINUITATE%20MEDICAMENTE/04.07.2025%20Notificari_.pdf"
    }
