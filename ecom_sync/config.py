from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _bool(v: str | None, default: bool) -> bool:
    return default if v is None or v == "" else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    mock_mode: bool = True
    mock_base_url: str = "http://127.0.0.1:8710"
    data_dir: Path = Path("data")
    log_json: bool = True

    gumroad_access_token: str = ""
    gumroad_webhook_token: str = ""
    shopify_shop: str = "demo-store.myshopify.com"
    shopify_access_token: str = ""
    shopify_api_version: str = "2024-10"
    shopify_webhook_secret: str = "shpss_demo_secret"

    airtable_token: str = ""
    airtable_base_id: str = "appDEMO00000000000"
    airtable_table: str = "Orders"
    airtable_max_rps: float = 4.5
    airtable_429_cooldown: float = 30.0

    retry_attempts: int = 4
    retry_base_delay: float = 1.0
    http_timeout: float = 20.0
    extra: dict = field(default_factory=dict)

    @property
    def gumroad_base(self) -> str:
        return f"{self.mock_base_url}/gumroad" if self.mock_mode else "https://api.gumroad.com"

    @property
    def shopify_base(self) -> str:
        return f"{self.mock_base_url}/shopify" if self.mock_mode else f"https://{self.shopify_shop}"

    @property
    def airtable_base(self) -> str:
        return f"{self.mock_base_url}/airtable" if self.mock_mode else "https://api.airtable.com"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "sync.db"


def load_settings(**overrides) -> Settings:
    load_dotenv()
    e = os.environ.get
    s = Settings(
        mock_mode=_bool(e("MOCK_MODE"), True),
        mock_base_url=e("MOCK_BASE_URL", "http://127.0.0.1:8710"),
        data_dir=Path(e("DATA_DIR", "data")),
        log_json=_bool(e("LOG_JSON"), True),
        gumroad_access_token=e("GUMROAD_ACCESS_TOKEN", "") or ("mock-gumroad-token"),
        gumroad_webhook_token=e("GUMROAD_WEBHOOK_TOKEN", "") or "demo-token",
        shopify_shop=e("SHOPIFY_SHOP", "demo-store.myshopify.com"),
        shopify_access_token=e("SHOPIFY_ACCESS_TOKEN", "") or "shpat_mock",
        shopify_api_version=e("SHOPIFY_API_VERSION", "2024-10"),
        shopify_webhook_secret=e("SHOPIFY_WEBHOOK_SECRET", "") or "shpss_demo_secret",
        airtable_token=e("AIRTABLE_TOKEN", "") or "patMOCK",
        airtable_base_id=e("AIRTABLE_BASE_ID", "appDEMO00000000000"),
        airtable_table=e("AIRTABLE_TABLE", "Orders"),
        airtable_max_rps=float(e("AIRTABLE_MAX_RPS", "4.5")),
        airtable_429_cooldown=float(e("AIRTABLE_429_COOLDOWN", "30")),
        retry_attempts=int(e("RETRY_ATTEMPTS", "4")),
        retry_base_delay=float(e("RETRY_BASE_DELAY", "1.0")),
        http_timeout=float(e("HTTP_TIMEOUT", "20")),
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
