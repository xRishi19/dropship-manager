import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db.connection import connect
from backend.services.financials.aggregation import summarize
from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual
from backend.services.sync.ebay_sync import store_orders
from factories import ebay_order

NOW = datetime(2026, 9, 24, 18, 30, tzinfo=timezone.utc)


def seed(db, costs, returns=None, cancelled=()):
    """costs: {order_id: cents or None}. None leaves the Amazon cost unknown.

    Seed synthetic orders with optional known Amazon costs, cancellations, and return overrides, then load their effective finances.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        costs: Mapping from synthetic order identifiers to Amazon costs in cents; None values
            remain unknown.
        returns: Optional mapping of order identifiers to manual return-status overrides.
        cancelled: Collection of order identifiers to mark cancelled in the seeded dataset.

    Returns:
        List of calculated ledger order dictionaries.
    """
    with db:
        store_orders(db, [ebay_order(oid, created='2026-09-24T14:00:00.000Z', cancelled=oid in cancelled)
                          for oid in costs], NOW)
        for oid, cents in costs.items():
            if cents is not None:
                manual.set_manual_cost(db, oid, cents, NOW)
        for oid, status in (returns or {}).items():
            manual.set_return_status(db, oid, status, NOW)
    return load_orders(db)


def test_config_default_rate(config):
    """Regression check: config default rate.

    Args:
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert config.accounting.prime_cashback_rate == 0.05


def test_cashback_is_five_percent_of_known_amazon_cost(db):
    """Regression check: cashback is five percent of known amazon cost.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    cards = summarize(seed(db, {'A': 2500, 'B': 1999}), 0.05)
    assert cards['amazon_cost_cents'] == 4499
    assert cards['prime_cashback_cents'] == round(4499 * 0.05) == 225
    assert cards['prime_cashback_rate'] == 0.05


@pytest.mark.parametrize('returns,cancelled', [({'B': 'REFUNDED'}, ()), ({'B': 'RETURN_COMPLETED'}, ()),
                                                ({}, ('B',))])
def test_refunded_returned_or_cancelled_order_contributes_nothing(db, returns, cancelled):
    """Regression check: refunded returned or cancelled order contributes nothing.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        returns: Optional mapping of order identifiers to manual return-status overrides.
        cancelled: Whether an order is cancelled, or the collection of identifiers to cancel in
            a seeded dataset.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    cards = summarize(seed(db, {'A': 2000, 'B': 3000}, returns, cancelled), 0.05)
    assert (cards['amazon_cost_cents'], cards['prime_cashback_cents']) == (2000, 100)


def test_unknown_cost_contributes_nothing(db):
    """Regression check: unknown cost contributes nothing.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    cards = summarize(seed(db, {'A': 2000, 'B': None}), 0.05)
    assert cards['awaiting_cost'] == 1
    assert (cards['amazon_cost_cents'], cards['prime_cashback_cents']) == (2000, 100)


def test_cashback_does_not_change_profit(db):
    """Regression check: cashback does not change profit.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    orders = seed(db, {'A': 2500, 'B': None, 'C': 1000}, {'C': 'REFUNDED'})
    without, with_rate = summarize(orders), summarize(orders, 0.05)
    assert without['prime_cashback_cents'] == 0 and with_rate['prime_cashback_cents'] == 125
    for key in ('profit_cents', 'profit_margin', 'avg_profit_per_order_cents', 'amazon_cost_cents',
                'net_revenue_cents'):
        assert without[key] == with_rate[key]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Construct an eBay client using this callable fake as its HTTP transport.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Yields:
        EbayClient instance backed by an HTTPX MockTransport.
    """
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    db = connect(config['database'])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    created = now.isoformat().replace('+00:00', 'Z')
    with db:
        store_orders(db, [ebay_order('A', created=created), ebay_order('B', created=created, name='Bob')], now)
        manual.set_manual_cost(db, 'A', 2150, now)
    db.close()
    with TestClient(app) as test_client:
        yield test_client


def test_overview_api_returns_prime_cashback(client):
    """Regression check: overview API returns prime cashback.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    cards = client.get('/api/overview?range=1w').json()['cards']
    assert cards['amazon_cost_cents'] == 2150
    assert (cards['prime_cashback_cents'], cards['prime_cashback_rate']) == (108, 0.05)  # round(107.5)
