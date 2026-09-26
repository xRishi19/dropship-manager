"""The $0.30 ordering-provider fee: charged on every eBay order, including cancelled/refunded/returned ones."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Config
from backend.db.connection import connect
from backend.services.financials.aggregation import series, summarize, time_range
from backend.services.financials.calculator import compute
from backend.services.financials.export import EXPORT_FIELDS, export_csv
from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual
from backend.services.sync.ebay_sync import store_orders, store_transactions
from factories import ebay_order, sale_transaction

REPO = Path(__file__).resolve().parent.parent
NY = ZoneInfo('America/New_York')
NOW = datetime(2026, 9, 24, 18, 30, tzinfo=timezone.utc)
FEE = 30


def calc(status='NONE', cancelled=False, revenue=4000, cost=2500, fee=FEE):
    return compute(is_cancelled=cancelled, return_status=status, net_revenue_cents=revenue, amazon_cost_cents=cost,
                   ordering_fee_cents=fee)


def test_fee_is_configured_at_30_cents():
    assert Config().accounting.ordering_fee_cents == 30
    assert json.loads((REPO / 'config.json').read_text())['accounting']['ordering_fee_cents'] == 30


def test_normal_order_profit_includes_fee_and_margin_roi_use_it():
    f = calc()
    assert (f.original_profit_cents, f.effective_profit_cents, f.ordering_fee_cents) == (1470, 1470, 30)
    assert (f.margin, f.roi) == (round(1470 / 4000, 4), round(1470 / 2500, 4))
    assert calc(fee=0).effective_profit_cents == 1500  # The calculator default is no fee.


@pytest.mark.parametrize('status,cancelled,label', [('NONE', True, 'Cancelled'),
                                                    ('REFUNDED', False, 'Refunded'),
                                                    ('RETURN_COMPLETED', False, 'Returned')])
def test_cancelled_refunded_returned_cost_the_fee(status, cancelled, label):
    f = calc(status, cancelled)
    assert (f.effective_revenue_cents, f.effective_cost_cents, f.effective_profit_cents) == (0, 0, -30)
    assert (f.original_profit_cents, f.display_status, f.excluded) == (1470, label, True)
    assert (f.margin, f.roi) == (None, None)


def test_unknown_cost_keeps_profit_unknown():
    f = calc(cost=None)
    assert (f.original_profit_cents, f.effective_profit_cents, f.cost_known) == (None, None, False)
    assert calc(status='RETURN_STARTED').effective_profit_cents == 1470  # Pending return keeps its profit.


def seeded(db, fee=FEE):
    orders = [ebay_order('A', created='2026-09-24T14:00:00.000Z'),  # costed: 4000 - 2500 - 30
              ebay_order('B', created='2026-09-24T03:00:00.000Z'),  # previous NY day, cost unknown
              ebay_order('C', created='2026-09-23T12:00:00.000Z', cancelled=True),
              ebay_order('D', created='2026-09-22T12:00:00.000Z'),  # refunded
              ebay_order('E', created='2026-09-21T12:00:00.000Z')]  # returned
    with db:
        store_orders(db, orders, NOW)
        store_transactions(db, [sale_transaction('A', 40.0)], NOW)
        manual.set_manual_cost(db, 'A', 2500, NOW)
        manual.set_manual_cost(db, 'C', 2500, NOW)
        manual.set_manual_cost(db, 'D', 1000, NOW)
        manual.set_return_status(db, 'D', 'REFUNDED', NOW)
        manual.set_return_status(db, 'E', 'RETURN_COMPLETED', NOW)
    return load_orders(db, ordering_fee_cents=fee)


def test_cards_profit_and_ordering_fees(db):
    cards = summarize(seeded(db), cashback_rate=0.05)
    assert cards['total_orders'] == 2 and cards['net_revenue_cents'] == 4000 + 3900  # C, D, E stay excluded.
    assert cards['excluded'] == {'cancelled': 1, 'returned': 1, 'refunded': 1}
    assert cards['profit_cents'] == 1470 - 3 * 30  # A, plus -fee for each excluded order; B awaits cost.
    assert cards['ordering_fees_cents'] == 30 * 4  # Costed included (A) + excluded (C, D, E).
    # Amazon cost and Prime cashback are Amazon spend only.
    assert (cards['amazon_cost_cents'], cards['prime_cashback_cents']) == (2500, 125)
    assert (cards['avg_profit_per_order_cents'], cards['profit_margin']) == (1470, round(1470 / 4000, 4))
    assert cards['awaiting_cost'] == 1


def test_cards_without_fee_match_old_rules(db):
    cards = summarize(seeded(db, fee=0))
    assert (cards['profit_cents'], cards['ordering_fees_cents']) == (1500, 0)


def test_series_profit_matches_cards(db):
    orders = seeded(db)
    _, _, granularity, buckets = time_range('1w', NOW, NY)
    points = series(orders, buckets, granularity, NY)
    cards = summarize(orders)
    assert sum(p['profit_cents'] for p in points) == cards['profit_cents']
    assert sum(p['orders'] for p in points) == cards['total_orders']
    assert sum(p['revenue_cents'] for p in points) == cards['net_revenue_cents']
    by_label = {p['label']: p for p in points}
    assert by_label['Sep 22']['orders'] == 0 and by_label['Sep 22']['profit_cents'] == -30  # Refunded D.
    assert by_label['Sep 21']['revenue_cents'] == 0 and by_label['Sep 21']['profit_cents'] == -30  # Returned E.


def test_csv_ordering_fee_column(db):
    rows = {r['ebay_order_id']: r for r in csv.DictReader(io.StringIO(export_csv(seeded(db))))}
    assert EXPORT_FIELDS.index('ordering_fee') == EXPORT_FIELDS.index('amazon_cost') + 1
    assert (rows['A']['amazon_cost'], rows['A']['ordering_fee'], rows['A']['effective_profit']) == ('25.00', '0.30', '14.70')
    assert (rows['C']['ordering_fee'], rows['C']['original_profit'], rows['C']['effective_profit']) == ('0.30', '13.70', '-0.30')
    assert (rows['B']['ordering_fee'], rows['B']['effective_profit']) == ('0.30', '')


@pytest.fixture
def api(tmp_path, monkeypatch):
    """App using config.json's accounting settings (fee 30) on a temp DB."""
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads((REPO / 'config.json').read_text())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    config['sync'] = dict(config['sync'], enabled=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    db = connect(config['database'])
    now = datetime.now(timezone.utc).replace(microsecond=0)
    stamp = lambda d: d.isoformat().replace('+00:00', 'Z')
    with db:
        store_orders(db, [ebay_order('A', created=stamp(now - timedelta(minutes=5))),
                          ebay_order('C', created=stamp(now - timedelta(minutes=10)), cancelled=True)], now)
        store_transactions(db, [sale_transaction('A', 40.0)], now)
        manual.set_manual_cost(db, 'A', 2500, now)
    db.close()
    with TestClient(app) as client:
        yield client


def test_api_overview_and_order_detail_include_fee(api):
    cards = api.get('/api/overview?range=1w').json()['cards']
    assert (cards['profit_cents'], cards['ordering_fees_cents']) == (1470 - 30, 60)
    detail = api.get('/api/orders/A').json()
    assert (detail['ordering_fee_cents'], detail['effective_profit_cents']) == (30, 1470)
    cancelled = api.get('/api/orders/C').json()
    assert (cancelled['ordering_fee_cents'], cancelled['effective_profit_cents'], cancelled['excluded']) == (30, -30, True)
    listed = {o['order_id']: o for o in api.get('/api/orders?range=1w').json()['orders']}
    assert listed['A']['ordering_fee_cents'] == 30 and listed['C']['effective_profit_cents'] == -30
    rows = {r['ebay_order_id']: r for r in csv.DictReader(io.StringIO(api.get('/api/orders/export?scope=all').text))}
    assert (rows['A']['ordering_fee'], rows['A']['effective_profit']) == ('0.30', '14.70')
