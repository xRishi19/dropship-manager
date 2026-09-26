"""The single eBay ingestion path used by the CLI, the accounting backfill and the sync loop.

Every writer here is idempotent: repeated or overlapping API results upsert by eBay's
own identifiers (order ID, line item ID, transaction ID, item ID) and never duplicate.
Callers own the transaction.
"""
import logging
from datetime import datetime, timedelta, timezone

from ...db.ingest import normalize, save_rows
from ...integrations.ebay.client import EbayClient
from ...integrations.ebay.mapping import aggregate_finances, order_sales_rows, parse_order, transaction_row
from ..returns.service import store_returns  # noqa: F401  (part of the sync API)

LOG = logging.getLogger(__name__)
ORDER_COLUMNS = ['order_id', 'created_at', 'last_modified_at', 'fulfillment_status', 'payment_status',
                 'cancel_state', 'is_cancelled', 'buyer_username', 'ship_name', 'ship_first_name', 'address1',
                 'address2', 'city', 'state', 'postal_code', 'country', 'currency', 'raw_json', 'synced_at']
ITEM_COLUMNS = ['order_id', 'line_item_id', 'item_id', 'sku', 'asin', 'title', 'quantity', 'line_total_cents', 'raw_json']
FINANCE_COLUMNS = ['order_id', 'currency', 'gross_total_cents', 'sales_tax_cents', 'final_value_fee_cents',
                   'other_fees_cents', 'other_fees_json', 'ad_fee_cents', 'refund_cents', 'net_revenue_cents', 'source']


def utc_now():
    return datetime.now(timezone.utc)


def stamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def upsert(db, table, columns, row, key):
    updates = ', '.join(f'{c}=excluded.{c}' for c in columns if c not in key)
    db.execute(f'INSERT INTO {table}({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))}) '
               f'ON CONFLICT({", ".join(key)}) DO UPDATE SET {updates}', [row[c] for c in columns])


def store_orders(db, orders, now=None, source='ebay'):
    """Upsert raw Fulfillment orders into orders/order_items, the estimated financial record
    and the sourcing `sales` rows. source='ebay_backfill' marks the accounting history backfill.
    (Full refunds are read from orders.payment_status by the returns service.)"""
    orders = list(orders)
    synced_at = stamp(now or utc_now())
    sales = []
    for order in orders:
        row, items, estimate = parse_order(order, synced_at)
        upsert(db, 'orders', ORDER_COLUMNS, row, ['order_id'])
        for item in items:
            upsert(db, 'order_items', ITEM_COLUMNS, item, ['order_id', 'line_item_id'])
        existing = db.execute('SELECT source FROM financial_records WHERE order_id=?', (row['order_id'],)).fetchone()
        if not existing or existing['source'] != 'finances':  # Posted finances are authoritative.
            upsert(db, 'financial_records', FINANCE_COLUMNS + ['updated_at'], {**estimate, 'updated_at': synced_at}, ['order_id'])
        sales += [normalize(r, 'orders') for r in order_sales_rows(order)]
    save_rows(db, sales, 'orders', source)
    # Payout transactions can post before the order itself is synced; apply them now.
    waiting = [o['orderId'] for o in orders
               if db.execute('SELECT 1 FROM ebay_transactions WHERE order_id=? LIMIT 1', (o['orderId'],)).fetchone()]
    refresh_finances(db, waiting, now)
    return len(orders)


def store_transactions(db, transactions, now=None):
    """Upsert Finances transactions, then rebuild the authoritative record of each touched order."""
    touched = set()
    for transaction in transactions:
        row = transaction_row(transaction)
        upsert(db, 'ebay_transactions', list(row), row, ['transaction_id', 'transaction_type'])
        if row['order_id']:
            touched.add(row['order_id'])
    return refresh_finances(db, touched, now)


def refresh_finances(db, order_ids, now=None):
    updated = 0
    for order_id in order_ids:
        if not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
            continue  # Kept in ebay_transactions; applied once the order itself is synced.
        rows = [dict(r) for r in db.execute('SELECT * FROM ebay_transactions WHERE order_id=?', (order_id,))]
        record = aggregate_finances(rows)
        if record:
            upsert(db, 'financial_records', FINANCE_COLUMNS + ['updated_at'],
                   {**record, 'order_id': order_id, 'updated_at': stamp(now or utc_now())}, ['order_id'])
            updated += 1
    return updated


def store_categories(db, api, limit=50):
    """Look up categories for sold items that don't have one yet (cached on listings)."""
    rows = db.execute('''SELECT DISTINCT oi.item_id FROM order_items oi JOIN listings l ON l.item_id=oi.item_id
                         WHERE l.category IS NULL LIMIT ?''', (limit,)).fetchall()
    failed = 0
    for row in rows:
        try:
            category = api.category(row['item_id'])
        except Exception as exc:  # e.g. multi-variation listings Browse can't resolve; don't retry forever.
            LOG.info('Category lookup failed for item %s: %s', row['item_id'], exc)
            category, failed = None, failed + 1
        with db:
            db.execute('UPDATE listings SET category=? WHERE item_id=?', (category or '', row['item_id']))
    return len(rows) - failed


def store_listings(db, listings, now=None):
    save_rows(db, listings, 'listings', 'ebay')
    return len(listings)


def sync(db, config, client=None, now=None):
    """CLI --import: the sourcing window of orders (by creation and by modification, which
    catches late cancellations) plus active/unsold listings, committed atomically."""
    now = now or utc_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=config.ebay.order_lookback_days - 1)
    api = client or EbayClient(config.ebay)
    try:
        LOG.info('Downloading eBay orders and active/unsold listings')
        orders = {o['orderId']: o for o in api.raw_orders(stamp(start), stamp(now))}
        orders.update({o['orderId']: o for o in api.raw_orders(stamp(start), stamp(now), 'lastmodifieddate')})
        listings = api.listings(now.date().isoformat())
        with db:
            store_listings(db, listings, now)
            store_orders(db, orders.values(), now)
            rows = [normalize(r, 'orders') for o in orders.values() for r in order_sales_rows(o)]
            db.execute('INSERT INTO syncs(start_date,end_date,order_rows,listing_rows) VALUES (?,?,?,?)',
                       (start.date().isoformat(), now.date().isoformat(), len(rows), len(listings)))
        return rows, listings
    finally:
        if client is None:
            api.close()
