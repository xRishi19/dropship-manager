import json
import sqlite3
from datetime import datetime, timezone

from backend.db.connection import connect
from backend.integrations.ebay.mapping import aggregate_finances, return_status, transaction_row
from backend.services.financials.ledger import load_orders
from backend.services.sourcing.analysis import analyze
from backend.services.sync.ebay_sync import store_orders, store_returns, store_transactions
from factories import charge, ebay_order, sale_transaction

NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def counts(db):
    return {t: db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]
            for t in ('orders', 'order_items', 'sales', 'financial_records', 'ebay_transactions')}


def test_duplicate_and_overlapping_syncs_never_duplicate(db):
    first = [ebay_order('A'), ebay_order('B', items=[('111', 'Lug Nuts', 1, ''), ('222', 'Spark Plugs', 2, '')])]
    with db:
        store_orders(db, first, NOW)
        store_orders(db, first, NOW)  # Same page twice.
        store_orders(db, [ebay_order('B', items=[('111', 'Lug Nuts', 1, ''), ('222', 'Spark Plugs', 2, '')]),
                          ebay_order('C')], NOW)  # Overlapping window.
    assert counts(db) == {'orders': 3, 'order_items': 4, 'sales': 4, 'financial_records': 3, 'ebay_transactions': 0}
    assert db.execute('SELECT sum(quantity) FROM sales').fetchone()[0] == 5
    with db:
        store_transactions(db, [sale_transaction('A', 39.0)], NOW)
        store_transactions(db, [sale_transaction('A', 39.0)], NOW)
    assert counts(db)['ebay_transactions'] == 1


def test_order_fields_asin_address_and_estimated_revenue(db):
    with db:
        store_orders(db, [ebay_order('A', name='jane q doe', city='Saint Louis', state='MO')], NOW)
    order = load_orders(db)[0]
    assert (order['ship_first_name'], order['city'], order['state'], order['postal_code']) == ('jane', 'Saint Louis', 'MO', '62704')
    assert order['asin'] == 'B0TESTASIN'
    # Estimate until Finances posts: totalDueSeller (excludes eBay-collected tax) - marketplace fee.
    assert (order['gross_total_cents'], order['sales_tax_cents'], order['net_revenue_cents']) == (5000, 500, 3900)
    assert order['revenue_source'] == 'fulfillment_estimate' and order['revenue_estimated']


def test_finances_are_authoritative_and_survive_later_order_syncs(db):
    with db:
        store_orders(db, [ebay_order('A')], NOW)
        store_transactions(db, [sale_transaction('A', 41.0, fvf=6.0, ad=0.0), charge('A', 2.5)], NOW)
        store_orders(db, [ebay_order('A', modified='2026-09-22T00:00:00.000Z')], NOW)  # Must not revert.
    order = load_orders(db)[0]
    assert order['revenue_source'] == 'finances'
    assert (order['net_revenue_cents'], order['ad_fee_cents'], order['final_value_fee_cents']) == (3850, 250, 600)


def test_finance_transactions_before_their_order_are_applied_later(db):
    with db:
        store_transactions(db, [sale_transaction('A', 40.0)], NOW)
        assert counts(db)['financial_records'] == 0
        store_orders(db, [ebay_order('A')], NOW)  # No resend of the transaction needed.
    order = load_orders(db)[0]
    assert (order['net_revenue_cents'], order['revenue_source']) == (4000, 'finances')


def test_aggregate_finances_nets_fees_ads_refunds_and_other_charges():
    rows = [transaction_row(t) for t in [
        sale_transaction('A', 44.0, gross=55.0, fvf=6.0, ad=1.0, tax=5.0),
        charge('A', 3.0, fee_type='AD_FEE'),
        charge('A', 10.0, kind='REFUND', fee_type=None),
        charge('A', 4.0, kind='SHIPPING_LABEL', fee_type=None)]]
    record = aggregate_finances(rows)
    assert record['net_revenue_cents'] == 4400 - 300 - 1000 - 400
    assert (record['final_value_fee_cents'], record['ad_fee_cents'], record['refund_cents']) == (600, 400, 1000)
    assert (record['other_fees_cents'], json.loads(record['other_fees_json'])) == (400, {'SHIPPING_LABEL': 400})
    assert (record['gross_total_cents'], record['sales_tax_cents']) == (5500, 500)
    assert aggregate_finances([transaction_row(charge('A', 3.0))]) is None  # No SALE yet.


def test_cancelled_order_counts_zero_units_for_sourcing(db):
    with db:
        store_orders(db, [ebay_order('A', items=[('111', 'Lug Nuts', 3, '')])], NOW)
        store_orders(db, [ebay_order('A', items=[('111', 'Lug Nuts', 3, '')], cancelled=True)], NOW)
    assert db.execute('SELECT quantity FROM sales').fetchone()[0] == 0
    assert load_orders(db)[0]['display_status'] == 'Cancelled'


def test_refund_and_return_statuses_from_ebay(db):
    with db:
        store_orders(db, [ebay_order('A', payment='FULLY_REFUNDED'), ebay_order('B'), ebay_order('C')], NOW)
        store_returns(db, [{'orderId': 'B', 'returnId': 'r1', 'state': 'ITEM_SHIPPED', 'status': 'RETURN_REQUESTED'},
                           {'orderId': 'C', 'returnId': 'r2', 'state': 'CLOSED', 'status': 'CLOSED',
                            'sellerTotalRefund': {'actualRefundAmount': {'value': '50.00'}}},
                           {'orderId': 'unknown', 'returnId': 'r3', 'state': 'CLOSED'}], NOW)
    statuses = {o['order_id']: o['return_status'] for o in load_orders(db)}
    assert statuses == {'A': 'REFUNDED', 'B': 'RETURN_STARTED', 'C': 'RETURN_COMPLETED'}
    assert return_status({'state': 'RETURN_REQUEST_CANCELLED'}) == 'RETURN_CANCELLED'
    assert return_status({'state': 'CLOSED', 'status': 'CLOSED'}) == 'RETURN_CANCELLED'  # Closed, no refund.


def test_old_accounting_orders_do_not_change_sourcing_candidates(db, config):
    recent = [ebay_order(f'R{i}', created='2026-09-10T12:00:00.000Z',
                         items=[(f'1{i}', f'garage door {noun}', 2, f'sku{i}')])
              for i, noun in enumerate(['cable', 'spring', 'roller', 'opener'])]
    with db:
        store_orders(db, recent, NOW)
    loose = config.model_copy(update={'min_lift': 0})
    before = analyze(db, loose, '2026-09-21')[0]
    old = [ebay_order(f'O{i}', created='2026-02-01T12:00:00.000Z', items=[(f'9{i}', f'garage door item{i} bracket', 1, f'old{i}')])
           for i in range(30)]
    with db:
        store_orders(db, old, NOW, source='ebay_backfill')  # The accounting history backfill.
    after = analyze(db, loose, '2026-09-21')[0]
    facts = lambda rows: [(c['keyword'], c['total_units_sold'], c['distinct_listings'], c['sell_through_lift']) for c in rows]
    assert facts(after) == facts(before)
    # Anything the sourcing sync itself knows about (source 'ebay') still counts exactly as before,
    # even once its sales age out of the window; a later regular sync of a backfilled order keeps it.
    with db:
        store_orders(db, [ebay_order('S', created='2026-02-01T12:00:00.000Z',
                                     items=[('800', 'garage door legacy bracket', 1, 'legacy')])], NOW)
        store_orders(db, old[:1], NOW)
    after_sync = {c['keyword']: c for c in analyze(db, loose, '2026-09-21')[0]}
    assert after_sync['garage door']['distinct_listings'] == 4 + 2
    assert db.execute("SELECT source FROM sales WHERE order_id='O0'").fetchone()[0] == 'ebay'
    with db:
        store_orders(db, old[:1], NOW, source='ebay_backfill')  # Backfill never downgrades provenance.
    assert db.execute("SELECT source FROM sales WHERE order_id='O0'").fetchone()[0] == 'ebay'


def test_migrations_back_up_existing_database_once_and_keep_data(tmp_path):
    path = tmp_path / 'sourcing.sqlite3'
    old = sqlite3.connect(path)
    old.executescript('''CREATE TABLE listings (item_id TEXT PRIMARY KEY, title TEXT NOT NULL, product_key TEXT NOT NULL,
                         first_seen TEXT NOT NULL);
                         CREATE TABLE weeks (week TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT);
                         INSERT INTO listings VALUES ('1', 'garage door', 'sku:v0a1b2c3dQjBURVNUQVNJTg', '2026-09-01');
                         INSERT INTO weeks VALUES ('2026-09-14', '{}', NULL);''')
    old.commit()
    old.close()
    db = connect(str(path))
    backups = list((tmp_path / 'backups').iterdir())
    assert len(backups) == 1
    assert db.execute('SELECT product_key FROM listings').fetchone()[0] == 'asin:B0TESTASIN'
    assert db.execute('SELECT count(*) FROM weeks').fetchone()[0] == 1
    assert [r[0] for r in db.execute('SELECT version FROM schema_migrations')] == [1, 2, 3]
    saved = sqlite3.connect(backups[0])
    assert saved.execute('SELECT product_key FROM listings').fetchone()[0].startswith('sku:')  # Pre-migration copy.
    saved.close()
    db.close()
    connect(str(path)).close()
    assert len(list((tmp_path / 'backups').iterdir())) == 1
