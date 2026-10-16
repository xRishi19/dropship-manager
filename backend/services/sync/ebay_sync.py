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
    """Obtain the current aware UTC time for eBay ingestion.

    Args:
        None.

    Returns:
        Timezone-aware UTC datetime.
    """
    return datetime.now(timezone.utc)


def stamp(moment):
    """Convert an ingestion timestamp to second-resolution UTC text.

    Args:
        moment: Aware datetime to format or assign to a local chart bucket.

    Returns:
        ISO timestamp ending in Z.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def upsert(db, table, columns, row, key):
    """Insert or replace supplied non-key fields using a caller-selected conflict key. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        table: Trusted database table name receiving the upsert.
        columns: Ordered trusted database column names to write.
        row: Mapping with a value for every named column; key columns determine conflict handling.
        key: Trusted conflict-key column names used for idempotent insertion.

    Returns:
        None; modifies the selected table using parameterized row values.
    """
    updates = ', '.join(f'{c}=excluded.{c}' for c in columns if c not in key)
    db.execute(f'INSERT INTO {table}({", ".join(columns)}) VALUES ({", ".join("?" * len(columns))}) '
               f'ON CONFLICT({", ".join(key)}) DO UPDATE SET {updates}', [row[c] for c in columns])


def store_orders(db, orders, now=None, source='ebay'):
    """Persist Fulfillment orders, items, estimated finances and sourcing sales without overwriting posted finances. Caller owns the transaction.

    Upsert raw Fulfillment orders into orders/order_items, the estimated financial record
    and the sourcing `sales` rows. source='ebay_backfill' marks the accounting history backfill.
    (Full refunds are read from orders.payment_status by the returns service.)

    Args:
        db: Open SQLite connection used for the query or write.
        orders: Iterable of raw eBay Fulfillment order dictionaries.
        now: Optional ingestion timestamp; None uses the current UTC time.
        source: Sales-ingestion provenance tag; defaults to ebay and uses ebay_backfill for accounting-only history.

    Returns:
        Number of input orders processed; previously received transactions are applied to newly imported orders.
    """
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
    """Persist Finances transactions and rebuild authoritative financial records for touched orders. Caller owns the transaction.

    Upsert Finances transactions, then rebuild the authoritative record of each touched order.

    Args:
        db: Open SQLite connection used for the query or write.
        transactions: Raw eBay Finances transaction dictionaries to ingest.
        now: Optional financial-record update timestamp; None uses the current UTC time.

    Returns:
        Number of known orders whose financial records were rebuilt.
    """
    touched = set()
    for transaction in transactions:
        row = transaction_row(transaction)
        upsert(db, 'ebay_transactions', list(row), row, ['transaction_id', 'transaction_type'])
        if row['order_id']:
            touched.add(row['order_id'])
    return refresh_finances(db, touched, now)


def refresh_finances(db, order_ids, now=None):
    """Reaggregate stored transactions for existing orders into authoritative posted finances. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        order_ids: Iterable of order identifiers to reaggregate; identifiers absent from orders are skipped.
        now: Optional financial-record update timestamp; None uses the current UTC time.

    Returns:
        Number of order financial records updated; unknown orders remain deferred.
    """
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
    """Fetch and cache missing sold-item categories and optional thumbnail metadata.

    Cache category and optional image metadata for sold items.

    Args:
        db: Open SQLite connection used for the query or write.
        api: eBay client used to fetch listing or item metadata.
        limit: Maximum number of distinct sold items attempted per call; defaults to 50.

    Returns:
        Number of successful lookups; commits each attempted lookup, including failure markers.
    """
    rows = db.execute('''SELECT DISTINCT oi.item_id FROM order_items oi JOIN listings l ON l.item_id=oi.item_id
                         WHERE l.category IS NULL OR l.image_checked=0 LIMIT ?''', (limit,)).fetchall()
    failed = 0
    for row in rows:
        try:
            if hasattr(api, 'item_metadata'):
                metadata = api.item_metadata(row['item_id']) or {}
                category, image = metadata.get('category'), metadata.get('image_url')
            else:
                category, image = api.category(row['item_id']), None
        except Exception as exc:  # e.g. multi-variation listings Browse can't resolve; don't retry forever.
            LOG.info('Category lookup failed for item %s: %s', row['item_id'], exc)
            category, image, failed = None, None, failed + 1
        with db:
            db.execute("UPDATE listings SET category=coalesce(nullif(?, ''), category, ''), "
                       'image_url=coalesce(?, image_url), image_checked=1 WHERE item_id=?',
                       (category, image, row['item_id']))
    return len(rows) - failed


def store_listings(db, listings, now=None):
    """Persist normalized listings through the shared idempotent ingestion path. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        listings: Listing dictionaries produced by shared ingestion or factual analysis.
        now: Compatibility timestamp argument; currently unused by the listing ingestion path.

    Returns:
        Number of supplied listing rows.
    """
    save_rows(db, listings, 'listings', 'ebay')
    return len(listings)


def sync(db, config, client=None, now=None):
    """Download CLI sourcing-window orders and active/unsold listings and commit them atomically.

    CLI --import: the sourcing window of orders (by creation and by modification, which
    catches late cancellations) plus active/unsold listings, committed atomically.

    Args:
        db: Open SQLite connection used for the query or write.
        config: Project configuration supplying database, integrations and business settings.
        client: Optional eBay client; an internally created client is closed after the import.
        now: Optional import-window upper timestamp; None uses the current UTC time.

    Returns:
        Tuple (normalized sales rows, listing rows); closes only an internally created eBay client.
    """
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
