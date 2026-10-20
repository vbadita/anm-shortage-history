"""Command line: python -m anm_collector {backfill,sync,report}."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
import uuid
from collections import Counter

import requests

from .collector import Collector, RunResult, build_report, write_candidates
from .config import Config
from .discovery import DiscoveryError
from .http import PoliteClient
from .logs import log, setup_logging
from .metrics import RunMetrics
from .storage import Catalog, LockHeld, Manifest, RawStore, RunLock

EXIT_OK, EXIT_DOWNLOAD_FAILED, EXIT_FATAL, EXIT_METRICS_PUSH_FAILED = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    """Entry point: any unexpected exception becomes one JSON log line and exit 2,
    never a raw traceback (unsearchable) or exit 1 (which means 'download failed')."""
    try:
        return _run(argv)
    except Exception as exc:
        log(
            "run_crashed",
            logging.ERROR,
            phase="unknown",
            error=repr(exc),
            traceback=traceback.format_exc(),
        )
        return EXIT_FATAL


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(prog="anm_collector")
    parser.add_argument("mode", choices=["backfill", "sync", "report"])
    parser.add_argument("-v", "--verbose", action="store_true", help="log every HTTP request")
    args = parser.parse_args(argv)

    run_id = uuid.uuid4().hex[:12]
    setup_logging(run_id, logging.DEBUG if args.verbose else logging.INFO)
    started = time.monotonic()

    try:
        cfg = Config.from_env()
    except ValueError as exc:
        log("config", logging.ERROR, phase="startup", result="invalid", error=str(exc))
        return EXIT_FATAL

    if args.mode == "report":
        print(json.dumps(build_report(Manifest(cfg.manifest_path).rows), indent=2))
        return EXIT_OK

    try:
        with RunLock(cfg.data_dir / ".lock"):
            return _collect(args.mode, cfg, started)
    except LockHeld as exc:
        log(
            "lock_held",
            logging.ERROR,
            phase="startup",
            mode=args.mode,
            result="skipped",
            error=str(exc),
        )
        return EXIT_FATAL


def _collect(mode: str, cfg: Config, started: float) -> int:
    """One run, called with the data-dir lock held (the manifest is read under it)."""
    manifest = Manifest(cfg.manifest_path)
    log(
        "run_start",
        phase="startup",
        mode=mode,
        data_dir=str(cfg.data_dir),
        manifest_rows=len(manifest.rows),
        pushgateway=cfg.pushgateway_url,
    )
    metrics = RunMetrics()
    client = PoliteClient(
        cfg.user_agent,
        cfg.min_interval_seconds,
        cfg.timeout_seconds,
        cfg.max_retries,
        on_request=metrics.on_request,
    )
    collector = Collector(client, Catalog(RawStore(cfg.raw_dir), manifest))

    result: RunResult | None = None
    discovered: int | None = None
    report = None
    try:
        result, cands = collector.backfill() if mode == "backfill" else collector.sync()
        discovered = len(cands)
        if mode == "backfill":
            write_candidates(cfg.data_dir / "candidates.csv", cands)
        report = build_report(manifest.rows, result)
        (cfg.data_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        exit_code = EXIT_OK if result.ok else EXIT_DOWNLOAD_FAILED
    # RequestException subclasses OSError, so it must be caught first.
    except (DiscoveryError, requests.RequestException) as exc:
        log("run_fatal", logging.ERROR, phase="discover", mode=mode, error=str(exc))
        exit_code = EXIT_FATAL
    except OSError as exc:  # permissions, disk full, read-only filesystem
        log("run_fatal", logging.ERROR, phase="storage", mode=mode, error=repr(exc))
        exit_code = EXIT_FATAL

    success = exit_code == EXIT_OK
    metrics.record_run(result.outcomes if result else Counter(), discovered, manifest.rows)
    metrics.finish(started, success)
    pushed = not cfg.pushgateway_url or metrics.push(cfg.pushgateway_url, mode, success)
    if not pushed and exit_code == EXIT_OK:  # a data failure is the more important signal
        exit_code = EXIT_METRICS_PUSH_FAILED

    log(
        "run_end",
        logging.INFO if exit_code == EXIT_OK else logging.ERROR,
        phase="report",
        mode=mode,
        result={0: "ok", 1: "download_failed", 2: "fatal", 3: "metrics_push_failed"}[exit_code],
        exit_code=exit_code,
        duration_s=round(time.monotonic() - started, 1),
        report=report,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
