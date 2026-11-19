import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ecom_sync.config import Settings  # noqa: E402
from ecom_sync.pipeline import build_engine  # noqa: E402
from mock_apis.server import MockState, create_mock_app  # noqa: E402


class FakeClock:
    """Shared virtual time: the client's sleeps advance the mock server's clock. Tests run instantly."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += max(s, 0)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def mock(clock):
    state = MockState(penalty_s=30, clock=clock.now)
    app = create_mock_app(state)
    return state, TestClient(app)


@pytest.fixture
def settings(tmp_path):
    return Settings(mock_mode=True, mock_base_url="http://testserver", data_dir=tmp_path, log_json=False,
                    gumroad_access_token="tok", gumroad_webhook_token="gum-secret",
                    shopify_access_token="shpat", shopify_webhook_secret="shh",
                    airtable_token="pat", retry_base_delay=0.5, airtable_429_cooldown=30)


@pytest.fixture
def engine(settings, mock, clock):
    _, http = mock
    eng = build_engine(settings, http=http, sleep=clock.sleep)
    eng.airtable.limiter.clock = clock.now
    return eng


def airtable_rows(state, key):
    return [r for r in state.airtable if r["fields"].get("Order ID") == key]
