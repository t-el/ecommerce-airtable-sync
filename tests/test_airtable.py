import httpx
import pytest

from ecom_sync.airtable import AirtableClient
from ecom_sync.ratelimit import RateLimiter
from ecom_sync.retry import PermanentError, TransientError, with_retry


def row(i, currency="USD"):
    return {"Order ID": f"test:{i}", "Source": "Shopify", "Currency": currency, "Status": "paid", "Gross": 1.0}


def client(mock, clock, rps=4.5):
    _, http = mock
    return AirtableClient(http, "http://testserver/airtable", "pat", "appX", "Orders", max_rps=rps,
                          cooldown_429=30, sleep=clock.sleep,
                          limiter=RateLimiter(rps, clock=clock.now, sleep=clock.sleep))


def test_limiter_spaces_requests(clock):
    lim = RateLimiter(5, clock=clock.now, sleep=clock.sleep)
    for _ in range(11):
        lim.acquire()
    assert clock.t - 1000.0 == pytest.approx(2.0)   # 11 calls at 5/s = 2 seconds of spacing


def test_upsert_batches_of_ten_and_creates_then_updates(mock, clock):
    state, _ = mock
    at = client(mock, clock)
    res = at.upsert([row(i) for i in range(23)])
    assert len(res.created) == 23 and not res.failed
    assert at.requests == 3                           # 10 + 10 + 3
    res2 = at.upsert([row(i) for i in range(23)])
    assert len(res2.updated) == 23 and not res2.created


def test_staying_under_limit_means_no_429(mock, clock):
    state, _ = mock
    at = client(mock, clock, rps=4.5)
    for i in range(30):
        at.upsert([row(i)])
    assert state.airtable_429s == 0 and at.rate_limited == 0


def test_without_limiter_429_triggers_30s_cooldown_and_still_completes(mock, clock):
    state, _ = mock
    at = client(mock, clock, rps=1000)                 # misconfigured: way over 5 req/s
    for i in range(12):
        at.upsert([row(i)])
    assert state.airtable_429s >= 1 and at.rate_limited >= 1
    assert clock.t - 1000.0 >= 30                      # waited out Airtable's 30 s penalty
    assert len([r for r in state.airtable if r["fields"]["Order ID"].startswith("test:")]) == 12


def test_one_bad_record_does_not_sink_the_batch(mock, clock):
    at = client(mock, clock)
    rows = [row(1), row(2, currency="JPY"), row(3)]
    res = at.upsert(rows)
    assert set(res.created) == {"test:1", "test:3"}
    assert "INVALID_MULTIPLE_CHOICE_OPTIONS" in res.failed["test:2"]


def test_list_records_follows_offset(mock, clock):
    state, _ = mock
    at = client(mock, clock)
    at.upsert([row(i) for i in range(150)])
    assert len(list(at.iter_records())) == len(state.airtable) == 157


def test_with_retry_does_not_retry_permanent_errors(clock):
    calls = []

    def boom():
        calls.append(1)
        raise PermanentError("HTTP 422", 422)

    with pytest.raises(PermanentError):
        with_retry(boom, step="x", sleep=clock.sleep)
    assert len(calls) == 1


def test_with_retry_gives_up_after_attempts(clock):
    def flaky():
        raise httpx.ConnectTimeout("timeout")

    with pytest.raises(TransientError):
        with_retry(flaky, step="x", attempts=3, base_delay=1, sleep=clock.sleep)
    assert len(clock.sleeps) == 2
