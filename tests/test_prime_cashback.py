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
    """costs: {order_id: cents or None}. None leaves the Amazon cost unknown."""
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
    assert config.accounting.prime_cashback_rate == 0.05


def test_cashback_is_five_percent_of_known_amazon_cost(db):
    cards = summarize(seed(db, {'A': 2500, 'B': 1999}), 0.05)
    assert cards['amazon_cost_cents'] == 4499
    assert cards['prime_cashback_cents'] == round(4499 * 0.05) == 225
    assert cards['prime_cashback_rate'] == 0.05


@pytest.mark.parametrize('returns,cancelled', [({'B': 'REFUNDED'}, ()), ({'B': 'RETURN_COMPLETED'}, ()),
                                                ({}, ('B',))])
def test_refunded_returned_or_cancelled_order_contributes_nothing(db, returns, cancelled):
    cards = summarize(seed(db, {'A': 2000, 'B': 3000}, returns, cancelled), 0.05)
    assert (cards['amazon_cost_cents'], cards['prime_cashback_cents']) == (2000, 100)


def test_unknown_cost_contributes_nothing(db):
    cards = summarize(seed(db, {'A': 2000, 'B': None}), 0.05)
    assert cards['awaiting_cost'] == 1
    assert (cards['amazon_cost_cents'], cards['prime_cashback_cents']) == (2000, 100)


def test_cashback_does_not_change_profit(db):
    orders = seed(db, {'A': 2500, 'B': None, 'C': 1000}, {'C': 'REFUNDED'})
    without, with_rate = summarize(orders), summarize(orders, 0.05)
    assert without['prime_cashback_cents'] == 0 and with_rate['prime_cashback_cents'] == 125
    for key in ('profit_cents', 'profit_margin', 'avg_profit_per_order_cents', 'amazon_cost_cents',
                'net_revenue_cents'):
        assert without[key] == with_rate[key]


@pytest.fixture
def client(tmp_path, monkeypatch):
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
    cards = client.get('/api/overview?range=1w').json()['cards']
    assert cards['amazon_cost_cents'] == 2150
    assert (cards['prime_cashback_cents'], cards['prime_cashback_rate']) == (108, 0.05)  # round(107.5)
