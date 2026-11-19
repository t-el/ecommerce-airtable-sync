"""Retry with exponential backoff + jitter. Honours Retry-After. 4xx (except 408/409/429) fail fast."""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

import httpx

log = logging.getLogger("ecom_sync.retry")
T = TypeVar("T")


class TransientError(Exception):
    """A failure worth retrying (timeout, 429, 5xx)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """A failure that will not fix itself (400/401/403/404/422). Goes to the dead-letter queue."""

    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def raise_for_status(resp: httpx.Response, what: str) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:300]
    if resp.status_code in RETRYABLE_STATUS:
        ra = resp.headers.get("Retry-After")
        raise TransientError(f"{what} HTTP {resp.status_code}", float(ra) if ra else None)
    raise PermanentError(f"{what} HTTP {resp.status_code}: {body}", resp.status_code, body)


def with_retry(
    fn: Callable[[], T],
    *,
    step: str,
    attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    min_delay_on_429: float = 0.0,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (TransientError, httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= attempts:
                log.error("retries exhausted", extra={"step": step, "attempt": attempt, "error": str(exc)})
                raise TransientError(f"{step}: retries exhausted: {exc}") from exc
            delay = min(max_delay, base_delay * 2 ** (attempt - 1)) * (0.5 + random.random() / 2)
            ra = getattr(exc, "retry_after", None)
            if ra is not None:
                delay = max(delay, ra)
            if "429" in str(exc):
                delay = max(delay, min_delay_on_429)
            log.warning("transient failure, retrying",
                        extra={"step": step, "attempt": attempt, "delay_s": round(delay, 2), "error": str(exc)})
            sleep(delay)
    raise AssertionError("unreachable")
