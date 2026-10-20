import itertools

import requests

from anm_collector.http import PoliteClient


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.now += s


class FakeSession:
    """Returns queued status codes; each request 'takes' 0.5s of fake time."""

    def __init__(self, clock, statuses):
        self.clock, self.statuses, self.headers, self.calls = clock, list(statuses), {}, []

    def get(self, url, timeout):
        self.calls.append(self.clock.now)
        self.clock.now += 0.5
        resp = requests.Response()
        resp.status_code = self.statuses.pop(0)
        return resp


def make(statuses, **kw):
    clock = FakeClock()
    session = FakeSession(clock, statuses)
    client = PoliteClient("ua", session=session, clock=clock, sleep=clock.sleep, **kw)
    return client, session, clock


def test_rate_limit_spaces_requests_by_min_interval():
    client, session, _ = make([200, 200, 200], min_interval=2.0)
    for _ in range(3):
        client.get("u", "live")
    gaps = [b - a for a, b in itertools.pairwise(session.calls)]
    # each request starts >= 2s after the previous one ENDED (0.5s duration)
    assert gaps == [2.5, 2.5]


def test_retries_5xx_then_succeeds_with_exponential_backoff(monkeypatch):
    monkeypatch.setattr("anm_collector.http.random.uniform", lambda a, b: 0.0)
    seen = []
    client, _, clock = make(
        [503, 502, 200], backoff_base=2.0, on_request=lambda s, c, d: seen.append(c)
    )
    assert client.get("u", "live").status_code == 200
    assert seen == ["503", "502", "200"]
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps  # 2 * 2**attempt


def test_404_is_not_retried():
    client, session, _ = make([404])
    assert client.get("u", "live").status_code == 404
    assert len(session.calls) == 1


def test_gives_up_after_max_retries():
    client, session, _ = make([500] * 3, max_retries=2)
    assert client.get("u", "live").status_code == 500
    assert len(session.calls) == 3


def test_stop_flag_raises_before_next_request():
    from anm_collector.http import STOP, Terminated

    client, session, _ = make([200, 200])
    client.get("u", "live")
    STOP.set()
    try:
        try:
            client.get("u", "live")
            raise AssertionError("expected Terminated")
        except Terminated:
            pass
        assert len(session.calls) == 1  # the second request was never sent
    finally:
        STOP.clear()
