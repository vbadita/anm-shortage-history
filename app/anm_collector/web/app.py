"""Read-only search over the parsed ANM history.

Public routes live under BASE_PATH (/anm), which is all nginx forwards.
/healthz and /metrics are outside it: reachable in-cluster only.
"""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from .. import db, queries
from ..logs import log

HERE = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ANM_DATA_DIR", "data"))
BASE_PATH = os.environ.get("ANM_BASE_PATH", "/anm").rstrip("/")
SOURCE_PAGE = (
    "https://www.anm.ro/medicamente-de-uz-uman/autorizare-medicamente/"
    "notificari-discontinuitate-medicamente/"
)

# Long-running process: Prometheus scrapes these directly (no Pushgateway), and the
# default registry's process_* metrics (memory, CPU, open fds) are meaningful here.
REQUESTS = Counter("anm_web_requests_total", "HTTP requests", ["route", "status"])
LATENCY = Histogram(
    "anm_web_request_duration_seconds",
    "Request latency",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
DATA_LATEST = Gauge(
    "anm_web_latest_version_timestamp_seconds",
    "Publication date of the newest version the web app serves",
)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # no public API docs
app.mount(f"{BASE_PATH}/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")  # autoescape on for .html


def ro_date(iso: str | None) -> str:
    """2026-10-09 -> 09.10.2026 (empty for missing dates)."""
    if not iso:
        return ""
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


templates.env.filters["ro_date"] = ro_date


def open_db() -> sqlite3.Connection:
    return db.connect(DATA_DIR / "anm.sqlite", readonly=True)


@app.middleware("http")
async def observe(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    # route template, not the raw URL, so label values stay bounded
    if route:
        label = route.path
    elif request.url.path.startswith(f"{BASE_PATH}/static/"):
        label = "static"  # served by a mount: no route object
    else:
        label = "unmatched"
    elapsed = time.perf_counter() - started
    REQUESTS.labels(label, str(response.status_code)).inc()
    LATENCY.labels(label).observe(elapsed)
    if label not in ("/healthz", "/metrics"):  # probes and scrapes would drown the log
        log(
            "http_request",
            phase="web",
            route=label,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 1),
            client_ip=request.headers.get("x-real-ip"),
            query_len=len(request.query_params.get("q", "")),
        )
    # Pages contain no scripts; forms post only to us; never framed.
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    return response


def common(conn: sqlite3.Connection) -> dict:
    vs = queries.versions(conn)
    latest = vs[-1] if vs else None
    return {
        "base": BASE_PATH,
        "latest": latest,
        "version_count": len(vs),
        "source_page": SOURCE_PAGE,
    }


@app.get(f"{BASE_PATH}/", response_class=HTMLResponse)
def search_page(request: Request, q: str = Query("", max_length=100)):
    with closing(open_db()) as conn:
        ctx = common(conn)
        results = queries.search(conn, q) if q.strip() else []
        listed_now = (
            conn.execute(
                "SELECT count(DISTINCT product_key) FROM rows WHERE sha256 = ?",
                (ctx["latest"].sha256,),
            ).fetchone()[0]
            if ctx["latest"]
            else 0
        )
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            **ctx,
            "q": q,
            "results": results,
            "listed_now": listed_now,
            "too_short": bool(q.strip()) and len(q.strip()) < 3,
            "max_results": queries.MAX_RESULTS,
        },
    )


@app.get(f"{BASE_PATH}/produs", response_class=HTMLResponse)
def product_page(request: Request, k: str = Query(..., max_length=400)):
    with closing(open_db()) as conn:
        ctx = common(conn)
        periods = queries.history(conn, k)
        first = conn.execute(
            "SELECT name, form, strength, dci, holder, country FROM rows r "
            "JOIN versions v USING (sha256) WHERE product_key = ? "
            "ORDER BY v.file_date DESC LIMIT 1",
            (k,),
        ).fetchone()
    if first is None:
        return templates.TemplateResponse(request, "notfound.html", ctx, status_code=404)
    return templates.TemplateResponse(
        request,
        "product.html",
        {
            **ctx,
            "p": first,
            "periods": list(reversed(periods)),
            "in_latest": bool(periods) and periods[-1].listed,
        },
    )


@app.get("/healthz", response_class=PlainTextResponse)
def healthz(response: Response):
    """Ready = the database opens and holds at least one version."""
    try:
        with closing(open_db()) as conn:
            vs = queries.versions(conn)
    except sqlite3.Error as exc:
        response.status_code = 503
        return f"db error: {exc}"
    if not vs:
        response.status_code = 503
        return "no versions parsed yet"
    DATA_LATEST.set(time.mktime(date.fromisoformat(vs[-1].file_date).timetuple()))
    return f"ok {len(vs)} versions, latest {vs[-1].file_date}"


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
