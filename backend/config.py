"""Validated, explicit configuration."""
import json
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Weights(StrictModel):
    units: float = Field(default=5, ge=0)
    recent_units: float = Field(default=3, ge=0)
    breadth: float = Field(default=2, ge=0)
    lift: float = Field(default=3, ge=0)
    concentration: float = Field(default=3, ge=0)


class Tiers(StrictModel):
    """Each day pairs one Proven keyword with one Explore keyword."""
    proven_min_units: int = Field(default=5, ge=2)
    proven_asins: int = Field(default=200, ge=1)
    explore_asins: int = Field(default=50, ge=1)
    explore_min_new_category: int = Field(default=4, ge=0, le=7)
    explore_cooldown_weeks: int = Field(default=4, ge=0)


class EbayConfig(StrictModel):
    order_lookback_days: int = Field(default=90, ge=1, le=90)
    site_id: int = Field(default=0, ge=0)
    trading_version: str = '1477'


class AccountingConfig(StrictModel):
    start_date: str = '2026-01-01'  # One-time backfill of orders, finances and Gmail.
    timezone: str | None = None  # IANA name for dashboard buckets; default: this Mac's zone.
    prime_cashback_rate: float = Field(default=0.05, ge=0, le=1)  # Prime Visa cashback on Amazon spend.
    ordering_fee_cents: int = Field(default=30, ge=0)  # Ordering-provider fee charged on every eBay order.


class SyncConfig(StrictModel):
    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=15)
    order_overlap_minutes: int = Field(default=5, ge=0)
    listings_refresh_hours: int = Field(default=24, ge=1)
    returns_interval_minutes: int = Field(default=10, ge=1)
    backfill_chunk_days: int = Field(default=30, ge=1, le=90)


class MatchingConfig(StrictModel):
    """Transparent Amazon-email scoring (0-100). See services/reconciliation/scoring.py."""
    window_hours: float = Field(default=24, gt=0)
    skew_minutes: float = Field(default=15, ge=0)
    recheck_hours: float = Field(default=26, gt=0)
    quantity_match: float = 25
    quantity_mismatch: float = -30
    first_name_match: float = 25
    first_name_mismatch: float = -15
    city_match: float = 20
    city_mismatch: float = -20
    state_match: float = 15
    time_max: float = 15
    category_match: float = 3
    # Amazon orders to these recipients (first names) are the seller's own purchases: never matched.
    personal_recipients: list[str] = []
    auto_match_score: float = 80
    auto_match_margin: float = 15
    review_score: float = 50


class GmailConfig(StrictModel):
    enabled: bool = True
    client_secret_path: str = 'secrets/gmail_client.json'
    token_path: str = 'data/gmail_token.json'
    query: str = 'from:auto-confirm@amazon.com subject:Ordered'


class Config(StrictModel):
    database: str = 'data/sourcing.sqlite3'
    reports_dir: str = 'reports'
    output_dir: str = 'output'
    model: str = 'gpt-5.4-mini'
    analysis_days: int = Field(default=90, ge=7)
    recent_days: int = Field(default=28, ge=1)
    min_total_units: int = Field(default=2, ge=2)
    min_successful_listings: int = Field(default=2, ge=2)
    min_successful_products: int = Field(default=2, ge=2)
    min_alnum_token_products: int = Field(default=5, ge=1)
    max_product_share: float = Field(default=.75, gt=0, le=1)
    min_lift: float = Field(default=1.0, ge=0)
    lift_prior_units: float = Field(default=2, gt=0)
    min_trend_units: int = Field(default=4, ge=0)
    candidate_limit: int = Field(default=200, ge=14, le=200)
    unigram_allowlist: list[str] = ['oem']
    blocked_phrases: list[str] = []
    weights: Weights = Weights()
    tiers: Tiers = Tiers()
    ebay: EbayConfig = EbayConfig()
    accounting: AccountingConfig = AccountingConfig()
    sync: SyncConfig = SyncConfig()
    matching: MatchingConfig = MatchingConfig()
    gmail: GmailConfig = GmailConfig()
    column_mapping: dict[str, str] = {}


def merge(base, override):
    """Nested dict merge: override's values win, sections merge key by key."""
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


def load_config(path: str) -> Config:
    """config.json, plus an optional sibling config.local.json (git-ignored) holding personal
    settings such as matching.personal_recipients, which is never shared."""
    path = Path(path)
    data = json.loads(path.read_text())
    local = path.with_name(path.stem + '.local' + path.suffix)
    if local.exists():
        data = merge(data, json.loads(local.read_text()))
    return Config.model_validate(data)
