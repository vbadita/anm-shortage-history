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
