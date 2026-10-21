import json

import pytest

from anm_collector import cli


@pytest.fixture
def run(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANM_PUSHGATEWAY_URL", raising=False)

    def _run(exc):
        def boom(self):
            raise exc

        monkeypatch.setattr(cli.Collector, "sync", boom)
        code = cli.main(["sync"])
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        return code, lines  # json.loads fails the test on any non-JSON line

    return _run


def test_storage_error_is_fatal_json(run):
    code, lines = run(PermissionError(13, "Permission denied", "/data/manifest.csv"))
    assert code == cli.EXIT_FATAL
    fatal = next(entry for entry in lines if entry["event"] == "run_fatal")
    assert fatal["phase"] == "storage" and "Permission denied" in fatal["error"]
    assert lines[-1]["event"] == "run_end" and lines[-1]["exit_code"] == 2


def test_unexpected_exception_is_json_not_traceback(run):
    code, lines = run(RuntimeError("bug"))
    assert code == cli.EXIT_FATAL
    assert lines[-1]["event"] == "run_crashed"
    assert "RuntimeError('bug')" in lines[-1]["error"]
    assert "Traceback" in lines[-1]["traceback"]


def test_lock_held_exits_2_without_touching_data(monkeypatch, tmp_path, capsys):
    from anm_collector.storage import RunLock

    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))
    with RunLock(tmp_path / ".lock"):
        code = cli.main(["sync"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == cli.EXIT_FATAL
    assert [entry["event"] for entry in lines] == ["lock_held"]
    assert not (tmp_path / "manifest.csv").exists()


def test_sigterm_mid_run_ends_cleanly(monkeypatch, tmp_path, capsys):
    import os
    import signal

    from anm_collector.http import STOP

    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))

    def sync(self):
        os.kill(os.getpid(), signal.SIGTERM)  # what the kubelet sends on pod deletion
        return self.client.get("https://example.invalid/", source="live")

    monkeypatch.setattr(cli.Collector, "sync", sync)
    try:
        code = cli.main(["sync"])
    finally:
        STOP.clear()
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert code == cli.EXIT_FATAL
    assert events == ["run_start", "run_terminated", "run_end"]  # no request was made


def _manifest_with(tmp_path, pdf_bytes):
    """A data dir whose manifest lists one stored version with the given bytes."""
    from anm_collector.storage import Catalog, Manifest, RawStore

    cat = Catalog(RawStore(tmp_path / "raw"), Manifest(tmp_path / "manifest.csv"))
    cat.ingest(pdf_bytes, url="https://x/a.pdf", file_date=None, source="live")


def test_parse_rebuild_swaps_database_atomically(monkeypatch, tmp_path, capsys):
    from pathlib import Path

    from anm_collector import db

    fixture = Path(__file__).parent / "fixtures" / "anm-2026-10-09-p1.pdf"
    _manifest_with(tmp_path, fixture.read_bytes())
    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANM_PUSHGATEWAY_URL", raising=False)
    assert cli.main(["parse"]) == cli.EXIT_OK
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert "db_swapped" in events
    assert not (tmp_path / "anm.sqlite.building").exists()
    conn = db.connect(tmp_path / "anm.sqlite", readonly=True)
    assert conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 36


def test_failed_rebuild_keeps_the_old_database(monkeypatch, tmp_path, capsys):
    from anm_collector import db

    old = db.connect(tmp_path / "anm.sqlite")
    old.execute("INSERT INTO versions VALUES ('old', '2020-01-01', 'p', 'u', 1, 1, 0, 1, 't')")
    old.commit()
    old.close()
    _manifest_with(tmp_path, b"%PDF-1.4\nnot really a pdf\n")  # passes magic bytes, unparseable
    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANM_PUSHGATEWAY_URL", raising=False)
    assert cli.main(["parse"]) == cli.EXIT_PARSE_FAILED
    parse_events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert any(e["event"] == "parse" and e["result"] == "error" for e in parse_events)
    conn = db.connect(tmp_path / "anm.sqlite", readonly=True)
    assert [r[0] for r in conn.execute("SELECT sha256 FROM versions")] == ["old"]
    assert not (tmp_path / "anm.sqlite.building").exists()


def test_successful_sync_records_last_check_failed_one_does_not(monkeypatch, tmp_path):
    from anm_collector.collector import RunResult

    monkeypatch.setenv("ANM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANM_PUSHGATEWAY_URL", raising=False)
    status = tmp_path / "last_sync.json"

    monkeypatch.setattr(cli.Collector, "sync", lambda self: (RunResult(failed=["u"]), {}))
    assert cli.main(["sync"]) == cli.EXIT_DOWNLOAD_FAILED
    assert not status.exists()

    monkeypatch.setattr(cli.Collector, "sync", lambda self: (RunResult(), {}))
    assert cli.main(["sync"]) == cli.EXIT_OK
    data = json.loads(status.read_text())
    assert data["mode"] == "sync" and data["last_success_utc"].endswith("+00:00")
