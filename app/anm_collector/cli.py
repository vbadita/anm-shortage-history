"""Command line: python -m anm_collector {backfill,sync,report}."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid

import requests

from .collector import Collector, build_report, write_candidates
from .config import Config
from .discovery import DiscoveryError
from .http import PoliteClient
from .logs import log, setup_logging
from .storage import Catalog, Manifest, RawStore

EXIT_OK, EXIT_DOWNLOAD_FAILED, EXIT_FATAL = 0, 1, 2


def main(argv: list[str] | None = None) -> int:
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

    manifest = Manifest(cfg.manifest_path)
    if args.mode == "report":
        print(json.dumps(build_report(manifest.rows), indent=2))
        return EXIT_OK

    log(
        "run_start",
        phase="startup",
        mode=args.mode,
        data_dir=str(cfg.data_dir),
        manifest_rows=len(manifest.rows),
    )
    client = PoliteClient(
        cfg.user_agent, cfg.min_interval_seconds, cfg.timeout_seconds, cfg.max_retries
    )
    collector = Collector(client, Catalog(RawStore(cfg.raw_dir), manifest))

    try:
        result, cands = collector.backfill() if args.mode == "backfill" else collector.sync()
    except (DiscoveryError, requests.RequestException) as exc:
        log(
            "run_end",
            logging.ERROR,
            phase="discover",
            mode=args.mode,
            result="fatal",
            error=str(exc),
            duration_s=round(time.monotonic() - started, 1),
        )
        return EXIT_FATAL

    if args.mode == "backfill":
        write_candidates(cfg.data_dir / "candidates.csv", cands)
    report = build_report(manifest.rows, result)
    (cfg.data_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    log(
        "run_end",
        logging.INFO if result.ok else logging.ERROR,
        phase="report",
        mode=args.mode,
        result="ok" if result.ok else "download_failed",
        duration_s=round(time.monotonic() - started, 1),
        report=report,
    )
    return EXIT_OK if result.ok else EXIT_DOWNLOAD_FAILED


if __name__ == "__main__":
    sys.exit(main())
