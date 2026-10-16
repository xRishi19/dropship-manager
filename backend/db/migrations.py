"""Numbered, additive schema migrations. Each runs once and is recorded in schema_migrations.

SQL stays portable (TEXT/INTEGER/REAL, ISO-8601 UTC timestamps, money as integer cents,
ON CONFLICT upserts) so the schema can move to Postgres later.
"""
from .ingest import asin_from_sku

SOURCING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS listings (
 item_id TEXT PRIMARY KEY, title TEXT NOT NULL, product_key TEXT NOT NULL,
 first_seen TEXT NOT NULL, start_date TEXT, end_date TEXT
);
CREATE TABLE IF NOT EXISTS sales (
 order_id TEXT NOT NULL, item_id TEXT NOT NULL REFERENCES listings(item_id),
 sold_date TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>=0),
 source TEXT NOT NULL, PRIMARY KEY(order_id,item_id)
);
CREATE INDEX IF NOT EXISTS sales_date ON sales(sold_date);
CREATE TABLE IF NOT EXISTS snapshots (
 item_id TEXT NOT NULL REFERENCES listings(item_id), observed_date TEXT NOT NULL,
 sold_quantity INTEGER NOT NULL CHECK(sold_quantity>=0), status TEXT NOT NULL,
 PRIMARY KEY(item_id,observed_date)
);
CREATE TABLE IF NOT EXISTS imports (
 digest TEXT PRIMARY KEY, filename TEXT NOT NULL, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS syncs (
 id INTEGER PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
 completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, order_rows INTEGER NOT NULL,
 listing_rows INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS weeks (
 week TEXT PRIMARY KEY, payload TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
'''

ACCOUNTING_SCHEMA = '''
CREATE TABLE IF NOT EXISTS orders (
 order_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, last_modified_at TEXT,
 fulfillment_status TEXT, payment_status TEXT, cancel_state TEXT,
 is_cancelled INTEGER NOT NULL DEFAULT 0, buyer_username TEXT,
 ship_name TEXT, ship_first_name TEXT, address1 TEXT, address2 TEXT, city TEXT, state TEXT,
 postal_code TEXT, country TEXT, currency TEXT, raw_json TEXT NOT NULL, synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_created ON orders(created_at);
CREATE TABLE IF NOT EXISTS order_items (
 order_id TEXT NOT NULL REFERENCES orders(order_id), line_item_id TEXT NOT NULL,
 item_id TEXT NOT NULL, sku TEXT, asin TEXT, title TEXT NOT NULL, quantity INTEGER NOT NULL,
 line_total_cents INTEGER, raw_json TEXT NOT NULL, PRIMARY KEY(order_id, line_item_id)
);
CREATE INDEX IF NOT EXISTS order_items_item ON order_items(item_id);
-- Raw eBay money per order. Effective (displayed) values are computed on read by
-- services.financials.calculator, so raw history is never overwritten by status changes.
CREATE TABLE IF NOT EXISTS financial_records (
 order_id TEXT PRIMARY KEY REFERENCES orders(order_id), currency TEXT,
 gross_total_cents INTEGER, sales_tax_cents INTEGER, final_value_fee_cents INTEGER,
 other_fees_cents INTEGER, other_fees_json TEXT, ad_fee_cents INTEGER, refund_cents INTEGER,
 net_revenue_cents INTEGER, source TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ebay_transactions (
 transaction_id TEXT NOT NULL, transaction_type TEXT NOT NULL, order_id TEXT,
 transaction_date TEXT NOT NULL, amount_cents INTEGER NOT NULL, booking_entry TEXT,
 raw_json TEXT NOT NULL, PRIMARY KEY(transaction_id, transaction_type)
);
CREATE INDEX IF NOT EXISTS ebay_transactions_order ON ebay_transactions(order_id);
CREATE TABLE IF NOT EXISTS amazon_email_purchases (
 gmail_message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, subject TEXT,
 first_name TEXT, city TEXT, state TEXT, quantity INTEGER, category TEXT,
 grand_total_cents INTEGER, parse_status TEXT NOT NULL, parse_error TEXT, body TEXT,
 fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS amazon_email_received ON amazon_email_purchases(received_at);
CREATE TABLE IF NOT EXISTS order_matches (
 order_id TEXT PRIMARY KEY REFERENCES orders(order_id), status TEXT NOT NULL,
 gmail_message_id TEXT UNIQUE REFERENCES amazon_email_purchases(gmail_message_id),
 confidence REAL, candidates_json TEXT, manual_cost_cents INTEGER, method TEXT,
 rejected_json TEXT, matched_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS returns (
 order_id TEXT PRIMARY KEY REFERENCES orders(order_id), ebay_status TEXT NOT NULL DEFAULT 'NONE',
 manual_status TEXT, ebay_return_id TEXT, raw_json TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
 name TEXT PRIMARY KEY, last_success_at TEXT, cursor TEXT, last_attempt_at TEXT,
 last_error TEXT, details_json TEXT
);
'''


def columns(db, table):
    """Inspect column names in a SQLite table without modifying it.

    Args:
        db: Open SQLite connection.
        table: Trusted schema table name interpolated into PRAGMA table_info.

    Returns:
        Set of existing column names; an absent table produces an empty set.
    """
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def m001_sourcing(db):
    """Create the original sourcing tables, add legacy listing date columns, and upgrade decodable SKU identities to ASIN keys.

    Args:
        db: SQLite connection supplied by the migration runner.

    Returns:
        None. Executes schema DDL and listing updates; version recording is owned by the migration runner.
    """
    db.executescript(SOURCING_SCHEMA)
    for column in ('start_date', 'end_date'):
        if column not in columns(db, 'listings'):
            db.execute(f'ALTER TABLE listings ADD COLUMN {column} TEXT')
    rows = db.execute("SELECT item_id, product_key FROM listings WHERE product_key LIKE 'sku:%'").fetchall()
    updates = [('asin:' + asin, row[0]) for row in rows if (asin := asin_from_sku(row[1][4:]))]
    db.executemany('UPDATE listings SET product_key=? WHERE item_id=?', updates)


def m002_accounting(db):
    """Create order, financial, email, matching, returns, and sync-state tables and add the listing category column if absent.

    Args:
        db: SQLite connection supplied by the migration runner.

    Returns:
        None. Executes schema changes; version recording is owned by the migration runner.
    """
    db.executescript(ACCOUNTING_SCHEMA)
    if 'category' not in columns(db, 'listings'):
        db.execute('ALTER TABLE listings ADD COLUMN category TEXT')


EXPENSES_SCHEMA = '''
CREATE TABLE IF NOT EXISTS expenses (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 start_date TEXT NOT NULL, recurring INTEGER NOT NULL DEFAULT 0, end_date TEXT NULL,
 created_at TEXT, updated_at TEXT
);
'''  # Dates are local calendar dates, YYYY-MM-DD.


def m003_expenses(db):
    """Create the expense table for one-time and monthly recurring charges.

    Args:
        db: SQLite connection supplied by the migration runner.

    Returns:
        None. Executes schema DDL; version recording is owned by the migration runner.
    """
    db.executescript(EXPENSES_SCHEMA)


def m004_notifications(db):
    """Create notification activation and per-order delivery tables and add cached listing image fields.

    Args:
        db: SQLite connection supplied by the migration runner; this migration must run only once.

    Returns:
        None. Executes additive schema changes; version recording is owned by the migration runner.
    """
    db.executescript("""
    CREATE TABLE notification_settings (name TEXT PRIMARY KEY, activated_at TEXT NOT NULL);
    CREATE TABLE sale_notifications (
        order_id TEXT PRIMARY KEY REFERENCES orders(order_id),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
        sent_at TEXT, message_id TEXT, last_error TEXT
    );
    """)
    db.execute('ALTER TABLE listings ADD COLUMN image_url TEXT')
    db.execute('ALTER TABLE listings ADD COLUMN image_checked INTEGER NOT NULL DEFAULT 0')


MIGRATIONS = [(1, 'sourcing', m001_sourcing), (2, 'accounting', m002_accounting), (3, 'expenses', m003_expenses),
              (4, 'notifications', m004_notifications)]
