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
    r = client.get("/anm/", params={"q": "substantia"})
    assert r.status_code == 200
    assert "ALFA 10 mg" in r.text and "BETA 10 mg" in r.text
    assert "În lista curentă" in r.text and "Nu mai apare" in r.text
    assert "01.02.2024" in r.text  # Romanian date format
    assert "/anm/produs?k=ALFA10MG%7CCOMPR.%7C10MG" in r.text  # key URL-encoded


def test_product_page_history_and_source_link(client):
    r = client.get("/anm/produs", params={"k": product_key("ALFA 10 mg", "COMPR.", "10mg")})
    assert r.status_code == 200
    assert "Nu apare în listă" in r.text  # absent in 2024-02-01
    assert 'href="https://www.anm.ro/_/x/2024-01-01.pdf"' in r.text


def test_unknown_product_is_404(client):
    assert client.get("/anm/produs", params={"k": "NOPE|X|Y"}).status_code == 404


def test_query_is_escaped_not_executed(client):
    r = client.get("/anm/", params={"q": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert (
        "script-src" not in r.headers["content-security-policy"]
    )  # default-src 'none' blocks scripts


def test_overlong_query_rejected(client):
    assert client.get("/anm/", params={"q": "x" * 101}).status_code == 422


def test_health_and_metrics_are_outside_the_public_prefix(client):
    assert client.get("/healthz").text.startswith("ok 2 versions, latest 2024-02-01")
    client.get("/anm/", params={"q": "alfa"})
    m = client.get("/metrics").text
    assert 'anm_web_requests_total{route="/anm/",status="200"}' in m
    assert client.get("/anm/metrics").status_code == 404  # not exposed under /anm


def test_healthz_503_without_database(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path / "empty")
    assert TestClient(web.app).get("/healthz").status_code == 503
