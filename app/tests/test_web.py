from datetime import date

import pytest
from fastapi.testclient import TestClient

from anm_collector import db
from anm_collector.parse import ParseResult, Row, product_key
from anm_collector.web import app as web


def row(name, notice_type, resume=""):
    return Row(
        page=1,
        nr="1",
        name=name,
        form="COMPR.",
        strength="10mg",
        holder="FIRMA SA",
        country="ROMANIA",
        dci="SUBSTANTIA",
        notice_date=date(2024, 1, 1),
        notice_date_raw="01.01.2024",
        notice_type=notice_type,
        resume_raw=resume,
        remarks="Motive comerciale",
        product_key=product_key(name, "COMPR.", "10mg"),
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    conn = db.connect(tmp_path / "anm.sqlite")
    for i, (d, rows) in enumerate(
        {
            "2024-01-01": [row("ALFA 10 mg", "Discontinuitate temporara", "mar.24")],
            "2024-02-01": [row("BETA 10 mg", "Discontinuitate permanenta")],
        }.items()
    ):
        db.store_version(
            conn,
            sha256=f"{i:064x}",
            file_date=d,
            path="raw/x.pdf",
            original_url=f"https://www.anm.ro/_/x/{d}.pdf",
            result=ParseResult(rows=rows),
        )
    conn.close()
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    return TestClient(web.app)


def test_search_shows_status_and_links(client):
    r = client.get("/istoric-lipsa-medicament-vaccin/", params={"q": "substantia"})
    assert r.status_code == 200
    assert "ALFA 10 mg" in r.text and "BETA 10 mg" in r.text
    assert "În lista curentă" in r.text and "Nu mai apare" in r.text
    assert "01.02.2024" in r.text  # Romanian date format
    assert (
        "/istoric-lipsa-medicament-vaccin/produs?k=ALFA10MG%7CCOMPR.%7C10MG" in r.text
    )  # key URL-encoded


def test_product_page_history_and_source_link(client):
    r = client.get(
        "/istoric-lipsa-medicament-vaccin/produs",
        params={"k": product_key("ALFA 10 mg", "COMPR.", "10mg")},
    )
    assert r.status_code == 200
    assert "Nu apare în listă" in r.text  # absent in 2024-02-01
    assert 'href="https://www.anm.ro/_/x/2024-01-01.pdf"' in r.text


def test_unknown_product_is_404(client):
    assert (
        client.get("/istoric-lipsa-medicament-vaccin/produs", params={"k": "NOPE|X|Y"}).status_code
        == 404
    )


def test_query_is_escaped_not_executed(client):
    r = client.get("/istoric-lipsa-medicament-vaccin/", params={"q": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert (
        "script-src" not in r.headers["content-security-policy"]
    )  # default-src 'none' blocks scripts


def test_overlong_query_rejected(client):
    assert (
        client.get("/istoric-lipsa-medicament-vaccin/", params={"q": "x" * 101}).status_code == 422
    )


def test_health_and_metrics_are_outside_the_public_prefix(client):
    assert client.get("/healthz").text.startswith("ok 2 versions, latest 2024-02-01")
    client.get("/istoric-lipsa-medicament-vaccin/", params={"q": "alfa"})
    m = client.get("/metrics").text
    assert 'anm_web_requests_total{route="/istoric-lipsa-medicament-vaccin/",status="200"}' in m
    assert (
        client.get("/istoric-lipsa-medicament-vaccin/metrics").status_code == 404
    )  # not exposed under the public prefix


def test_healthz_503_without_database(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path / "empty")
    assert TestClient(web.app).get("/healthz").status_code == 503


def test_typo_is_auto_corrected_not_reported_as_no_shortage(client):
    r = client.get(
        "/istoric-lipsa-medicament-vaccin/", params={"q": "alffa"}
    )  # one letter off ALFA
    assert "Rezultate pentru <strong>ALFA</strong>" in r.text
    assert "ALFA 10 mg" in r.text


def test_unknown_name_does_not_claim_no_shortage(client):
    r = client.get("/istoric-lipsa-medicament-vaccin/", params={"q": "zzzzqqq"})
    assert "Niciun rezultat" in r.text
    assert "Verificați scrierea" in r.text
    assert "nu are discontinuitate" not in r.text


B = web.BASE_PATH


def test_sitemap_lists_home_and_products_with_canonical_urls(client):
    import xml.etree.ElementTree as ET

    r = client.get(f"{B}/sitemap.xml")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = {
        u.find("s:loc", ns).text: u.find("s:lastmod", ns).text
        for u in ET.fromstring(r.content).findall("s:url", ns)
    }
    home = f"{web.PUBLIC_ORIGIN}{B}/"
    alfa = web.product_url(product_key("ALFA 10 mg", "COMPR.", "10mg"))
    assert urls[home] == "2024-02-01"  # newest version
    assert urls[alfa] == "2024-01-01"  # last version listing ALFA
    assert len(urls) == 3  # home + 2 products
    # the sitemap URL is exactly the canonical URL of the page
    page = client.get(alfa.removeprefix(web.PUBLIC_ORIGIN)).text
    assert f'<link rel="canonical" href="{alfa}">' in page


def test_indexing_rules(client):
    home = client.get(f"{B}/").text
    assert (
        '<link rel="canonical"' in home and 'name="description"' in home and "noindex" not in home
    )
    results = client.get(f"{B}/", params={"q": "alfa"}).text
    assert '<meta name="robots" content="noindex, follow">' in results
    missing = client.get(f"{B}/produs", params={"k": "NOPE"}).text
    assert "noindex" in missing


def test_product_description_summarises_status(client):
    r = client.get(f"{B}/produs", params={"k": product_key("ALFA 10 mg", "COMPR.", "10mg")})
    assert "<title>ALFA 10 mg \N{EN DASH} istoric discontinuitate · ANM</title>" in r.text
    assert "nu mai apare în lista ANM de discontinuități din 01.01.2024" in r.text
    assert "reluare estimată mar.24" in r.text
