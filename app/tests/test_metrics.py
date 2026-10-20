import time
from collections import Counter

from prometheus_client import generate_latest

from anm_collector.metrics import RunMetrics
from anm_collector.storage import ManifestRow


def row(status="new", downloaded_at="2026-10-20T16:05:00+00:00"):
    return ManifestRow(
        "2026-10-09",
        "u",
        "live",
        "",
        "ab" * 32,
        10,
        "application/pdf",
        downloaded_at,
        status,
        "D",
        "raw/x.pdf",
    )


def exposition(m: RunMetrics) -> str:
    return generate_latest(m.registry).decode()


def test_success_sets_last_success_and_counts():
    m = RunMetrics()
    m.on_request("live", "200", 0.3)
    m.on_request("cdx", "503", 1.2)
    m.record_run(
        Counter({"live:new": 2, "wayback:invalid": 1}),
        5,
        [row(), row(downloaded_at="2026-10-21T00:00:00+00:00"), row("duplicate")],
    )
    m.finish(time.monotonic() - 1.5, success=True)
    text = exposition(m)
    assert 'anm_requests_total{source="cdx",status_code="503"} 1.0' in text
    assert 'anm_downloads_total{result="new",source="live"} 2.0' in text
    assert 'anm_downloads_total{result="failed",source="live"} 0.0' in text  # explicit zero
    assert "anm_files_discovered 5.0" in text
    assert "anm_versions_unique 2.0" in text  # duplicates are not versions
    assert "anm_last_new_version_timestamp_seconds 1.7925408e+09" in text  # 2026-10-21T00:00Z
    assert m.last_success._value.get() > 1.7e9


def test_failed_run_pushes_no_last_success(monkeypatch):
    pushed = {}

    def fake_pushadd(gateway, job, grouping_key, registry, timeout):
        pushed.update(job=job, key=grouping_key, text=generate_latest(registry).decode())

    monkeypatch.setattr("anm_collector.metrics.pushadd_to_gateway", fake_pushadd)
    m = RunMetrics()
    m.record_run(Counter({"live:failed": 1}), 1, [])
    m.finish(time.monotonic(), success=False)
    assert m.push("http://pgw:9091", "sync", success=False)
    assert pushed["job"] == "anm_collector" and pushed["key"] == {"mode": "sync"}
    # not even a 0 sample: an unset unlabelled gauge would otherwise export 0
    assert "anm_last_success_timestamp_seconds" not in pushed["text"]
    assert "anm_run_duration_seconds" in pushed["text"]


def test_push_failure_returns_false():
    m = RunMetrics()
    assert m.push("http://127.0.0.1:9", "sync", success=True) is False
