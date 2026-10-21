"""python -m anm_collector.web  ->  uvicorn on $PORT (default 8080)."""

import os

import uvicorn

from ..logs import setup_logging

setup_logging(run_id="web")
uvicorn.run(
    "anm_collector.web.app:app",
    host="0.0.0.0",  # inside the pod; reachable only through the Service
    port=int(os.environ.get("PORT", "8080")),
    access_log=False,  # replaced by one JSON line per request (middleware)
    proxy_headers=False,  # client IP comes from nginx's X-Real-IP, logged explicitly
    log_level="warning",
)
