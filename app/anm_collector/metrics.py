"""Prometheus metrics for one run, pushed to the Pushgateway at the end.

Counters here count THIS run only (each run is a new process), so on the
Pushgateway they read as "last run's totals", not as ever-growing counters.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime

from prometheus_client import (
    CollectorRegistry,
    Gauge,
    Histogram,
    disable_created_metrics,
    pushadd_to_gateway,
)
from prometheus_client import Counter as PromCounter

from .logs import log
from .storage import ManifestRow

JOB = "anm_collector"

# *_created series help detect resets of long-lived counters; ours live for one run.
disable_created_metrics()
DOWNLOAD_RESULTS = ("new", "duplicate", "failed", "invalid")


class RunMetrics:
    def __init__(self) -> None:
        # Private registry: no python_gc/process metrics of a process that is about to exit.
        self.registry = CollectorRegistry()
        r = self.registry
        self.requests = PromCounter(
            "anm_requests_total", "HTTP requests made", ["source", "status_code"], registry=r
        )
        self.request_duration = Histogram(
            "anm_request_duration_seconds",
            "HTTP request duration",
            ["source"],
            buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
            registry=r,
        )
        self.files_discovered = Gauge(
            "anm_files_discovered", "Candidate file URLs found by discovery", registry=r
        )
        self.versions_unique = Gauge(
            "anm_versions_unique", "Distinct file versions stored (by sha256)", registry=r
        )
        self.downloads = PromCounter(
            "anm_downloads_total", "Download outcomes", ["source", "result"], registry=r
        )
        self.last_success = Gauge(
            "anm_last_success_timestamp_seconds",
            "Unix time of the last successful run (only set on success)",
            registry=r,
        )
        self.last_new_version = Gauge(
            "anm_last_new_version_timestamp_seconds",
            "Unix time a new file version was last downloaded",
            registry=r,
        )
        self.run_duration = Gauge("anm_run_duration_seconds", "Wall time of the run", registry=r)

    # PoliteClient hook: called once per HTTP attempt (retries included)
    def on_request(self, source: str, status_code: str, duration: float) -> None:
        self.requests.labels(source, status_code).inc()
        self.request_duration.labels(source).observe(duration)

    def record_run(
        self, outcomes: Counter[str], files_discovered: int | None, rows: list[ManifestRow]
    ) -> None:
        for key, n in outcomes.items():
            source, result = key.split(":", 1)
            self.downloads.labels(source, result).inc(n)
        # Pre-create the zero series for live/wayback so "failed == 0" is a
        # real 0 sample, not a missing series (absent data never alerts).
        for source in ("live", "wayback"):
            for result in DOWNLOAD_RESULTS:
                self.downloads.labels(source, result)
        if files_discovered is not None:
            self.files_discovered.set(files_discovered)
        stored = [r for r in rows if r.status == "new"]
        self.versions_unique.set(len(stored))
        if stored:
            newest = max(datetime.fromisoformat(r.downloaded_at) for r in stored)
            self.last_new_version.set(newest.timestamp())

    def finish(self, started_monotonic: float, success: bool) -> None:
        self.run_duration.set(time.monotonic() - started_monotonic)
        if success:
            self.last_success.set_to_current_time()

    def push(self, gateway: str, mode: str, success: bool) -> bool:
        """POST (pushadd), not PUT: only the metric names in this push are
        replaced. A failed run carries no last_success sample, so the previous
        success timestamp survives and keeps ageing -> freshness alert fires."""
        if not success:
            self.registry.unregister(self.last_success)
        try:
            pushadd_to_gateway(
                gateway, job=JOB, grouping_key={"mode": mode}, registry=self.registry, timeout=10
            )
        except OSError as exc:  # URLError, connection refused, timeout
            log(
                "metrics_push",
                logging.ERROR,
                phase="metrics",
                url=gateway,
                result="failed",
                error=str(exc),
            )
            return False
        log("metrics_push", phase="metrics", url=gateway, result="ok", mode=mode)
        return True
