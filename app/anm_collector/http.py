"""Rate-limited HTTP client with exponential-backoff retries."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

import requests

from .logs import log

# Worth retrying: the server may recover. 404/403/etc. are final answers.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504, 520, 522, 524})

# on_request(source, status_code_or_"error", duration_seconds) -> metrics in Phase 2
RequestHook = Callable[[str, str, float], None]


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        min_interval: float = 2.0,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        on_request: RequestHook | None = None,
        session: requests.Session | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.on_request = on_request
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._clock = clock
        self._sleep = sleep
        self._last_request_end: float | None = None

    def _wait_turn(self) -> None:
        """Global limiter: the gap between two requests is >= min_interval."""
        if self._last_request_end is None:
            return
        delay = self._last_request_end + self.min_interval - self._clock()
        if delay > 0:
            self._sleep(delay)

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 300.0)
        return self.backoff_base * (2**attempt) + random.uniform(0, 1)

    def get(self, url: str, source: str) -> requests.Response:
        """GET with retries. Returns the final response (any status) or raises
        requests.RequestException if every attempt failed at the network level."""
        last_exc: requests.RequestException | None = None
        resp: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_turn()
            start = self._clock()
            try:
                resp = self.session.get(url, timeout=(10, self.timeout))
                status = str(resp.status_code)
                last_exc = None
            except requests.RequestException as exc:
                resp, status, last_exc = None, "error", exc
            finally:
                self._last_request_end = self._clock()
            duration = self._last_request_end - start
            if self.on_request:
                self.on_request(source, status, duration)
            log(
                "http_request",
                logging.DEBUG,
                phase="http",
                url=url,
                source=source,
                status=status,
                duration_s=round(duration, 3),
                attempt=attempt,
            )

            if resp is not None and resp.status_code not in RETRYABLE_STATUS:
                return resp
            if attempt == self.max_retries:
                break
            wait = self._backoff(attempt, resp.headers.get("Retry-After") if resp else None)
            log(
                "http_retry",
                logging.WARNING,
                phase="http",
                url=url,
                source=source,
                result=status,
                error=str(last_exc) if last_exc else None,
                attempt=attempt + 1,
                wait_s=round(wait, 1),
            )
            self._sleep(wait)

        if resp is not None:
            return resp
        assert last_exc is not None
        raise last_exc
