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
