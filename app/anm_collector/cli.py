"""Command line: python -m anm_collector {backfill,sync,parse,report,search}."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
import traceback
import uuid
from collections import Counter
from contextlib import closing

import requests

from . import db, queries
from .collector import Collector, RunResult, build_report, write_candidates
from .config import Config
from .discovery import DiscoveryError
from .http import STOP, PoliteClient, Terminated
from .ingest import ParseRun, parse_pending
from .logs import log, setup_logging
from .metrics import RunMetrics
from .storage import Catalog, LockHeld, Manifest, RawStore, RunLock

EXIT_OK, EXIT_DOWNLOAD_FAILED, EXIT_FATAL, EXIT_METRICS_PUSH_FAILED, EXIT_PARSE_FAILED = (
    0,
    1,
    2,
    3,
    4,
)
RESULT_NAMES = {
    0: "ok",
    1: "download_failed",
    2: "fatal",
    3: "metrics_push_failed",
    4: "parse_failed",
}


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
    parser.add_argument("mode", choices=["backfill", "sync", "parse", "report", "search"])
    parser.add_argument("query", nargs="?", help="search: brand name, DCI or holder")
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
    if args.mode == "search":
        return _search(cfg, args.query or "")

    try:
        # Python ignores SIGTERM as PID 1 (no default action for init); without a
        # handler the kubelet waits terminationGracePeriodSeconds, then SIGKILLs.
        signal.signal(signal.SIGTERM, lambda signum, frame: STOP.set())
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
    parse_run: ParseRun | None = None
    discovered: int | None = None
    report = None
    try:
        if mode != "parse":
            result, cands = collector.backfill() if mode == "backfill" else collector.sync()
            discovered = len(cands)
            if mode == "backfill":
                write_candidates(cfg.data_dir / "candidates.csv", cands)
            report = build_report(manifest.rows, result)
            (cfg.data_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        exit_code = EXIT_OK if result is None or result.ok else EXIT_DOWNLOAD_FAILED
        # sync parses only what it downloaded (fast, fits its deadline); backfill and
        # parse handle everything pending (e.g. after a PARSER_VERSION bump).
        only = set(result.new_sha256) if mode == "sync" and result else None
        if only is None or only:
            # closing(): `with conn` alone commits but does NOT close the connection
            with closing(db.connect(cfg.db_path)) as conn:
                parse_run = parse_pending(cfg.data_dir, manifest.rows, conn, only=only)
            if parse_run.failed and exit_code == EXIT_OK:
                exit_code = EXIT_PARSE_FAILED
    except Terminated:
        log(
            "run_terminated",
            logging.WARNING,
            phase="shutdown",
            mode=mode,
            result="stopped",
            manifest_rows=len(manifest.rows),
        )
        exit_code = EXIT_FATAL
    # RequestException subclasses OSError, so it must be caught first.
    except (DiscoveryError, requests.RequestException) as exc:
        log("run_fatal", logging.ERROR, phase="discover", mode=mode, error=str(exc))
        exit_code = EXIT_FATAL
    except OSError as exc:  # permissions, disk full, read-only filesystem
        log("run_fatal", logging.ERROR, phase="storage", mode=mode, error=repr(exc))
        exit_code = EXIT_FATAL

    success = exit_code == EXIT_OK
    metrics.record_run(result.outcomes if result else Counter(), discovered, manifest.rows)
    if parse_run:
        metrics.record_parse(len(parse_run.parsed), len(parse_run.failed), parse_run.pending_after)
    metrics.finish(started, success)
    pushed = not cfg.pushgateway_url or metrics.push(cfg.pushgateway_url, mode, success)
    if not pushed and exit_code == EXIT_OK:  # a data failure is the more important signal
        exit_code = EXIT_METRICS_PUSH_FAILED

    log(
        "run_end",
        logging.INFO if exit_code == EXIT_OK else logging.ERROR,
        phase="report",
        mode=mode,
        result=RESULT_NAMES[exit_code],
        exit_code=exit_code,
        duration_s=round(time.monotonic() - started, 1),
        report=report,
        parsed=len(parse_run.parsed) if parse_run else 0,
        parse_failed=len(parse_run.failed) if parse_run else 0,
        unparsed=parse_run.pending_after if parse_run else None,
    )
    return exit_code


def _search(cfg: Config, query: str) -> int:
    """Human-readable search for the terminal (the web app uses the same queries)."""
    if not cfg.db_path.exists():
        print("no database yet: run `parse` first", file=sys.stderr)
        return EXIT_FATAL
    conn = db.connect(cfg.db_path, readonly=True)
    found = queries.search(conn, query)
    print(f"{len(found)} product(s) for {query!r}")
    for p in found:
        status = "LISTED NOW" if p.in_latest else f"not in current file (last {p.last_seen})"
        print(f"\n* {p.name} | {p.form} | {p.strength} | {p.dci} | {p.holder}")
        print(f"  {status}; listed in {p.versions_listed} version(s) since {p.first_seen}")
        for per in queries.history(conn, p.product_key):
            span = per.first if per.first == per.last else f"{per.first} .. {per.last}"
            if not per.listed:
                print(f"    {span:<24} -- not listed ({per.versions} version(s)) --")
                continue
            resume = f", reluare: {per.resume_raw}" if per.resume_raw else ""
            print(
                f"    {span:<24} {per.notice_type} (notificare {per.notice_date}){resume}"
                f" - {per.remarks}"
            )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
