"""Strict CSV/XLSX normalization, shared by eBay download adapters."""
import base64
import binascii
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ALIASES = {
 'item_id': ['item id', 'item number', 'ebay item id'],
 'title': ['title', 'item title', 'listing title'],
 'order_id': ['order id', 'order number'],
 'line_id': ['line id', 'transaction id', 'line item id'],
 'quantity': ['quantity', 'quantity sold'],
 'sold_quantity': ['sold quantity'],
 'sold_date': ['sale date', 'sold date', 'creation date'],
 'observed_date': ['snapshot date', 'observed date'],
 'sku': ['sku', 'custom label', 'custom label sku'],
 'status': ['status', 'order status', 'listing status'],
 'start_date': ['start date', 'listing start date'],
 'end_date': ['end date', 'listing end date'],
}
# Some sourcing tools write SKUs as an 8-hex prefix plus the base64-encoded Amazon ASIN.
SKU_ASIN = re.compile(r'v[0-9a-f]{8}([A-Za-z0-9+/]{14}(?:==)?)')
ASIN = re.compile(r'[A-Z0-9]{10}')


def header(value):
    return re.sub(r'[^a-z0-9]+', ' ', str(value).strip().lower()).strip()


def day(value) -> str:
    if isinstance(value, datetime):
        if value.tzinfo:
            value = value.astimezone(timezone.utc)
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return day(datetime.fromisoformat(text.replace('Z', '+00:00')))
    except ValueError:
        for fmt in ('%b-%d-%y', '%b-%d-%Y', '%m/%d/%Y', '%Y-%m-%d'):
            try:
                return datetime.strptime(text, fmt).date().isoformat()
            except ValueError:
                continue
    raise ValueError(f'Unsupported date {text!r}; use ISO YYYY-MM-DD')


def integer(value) -> int:
    text = str(value).strip()
    if not re.fullmatch(r'\d+', text):
        raise ValueError(f'Expected nonnegative integer, got {text!r}')
    return int(text)


def identifier(value) -> str:
    if isinstance(value, float):
        raise ValueError('Identifiers must be text, not floating point Excel cells')
    text = str(value).strip()
    if not text or re.search(r'^\d+\.\d+E[+-]?\d+$', text, re.I):
        raise ValueError('Missing or scientific-notation identifier')
    return text


def normalize(row, kind, snapshot_date=None):
    item = identifier(row.get('item_id', ''))
    title = str(row.get('title', '')).strip()
    if not title:
        raise ValueError('Missing title')
    result = {'item_id': item, 'title': title, 'sku': str(row.get('sku', '')).strip()}
    if kind == 'orders':
        result.update(order_id=identifier(row.get('order_id', '')),
                      line_id=str(row.get('line_id', '')).strip(),
                      sold_date=day(row.get('sold_date', '')),
                      quantity=integer(row.get('quantity', '')))
        if str(row.get('status', '')).lower() in {'cancelled', 'canceled'}:
            result['quantity'] = 0
    else:
        result.update(observed_date=day(row.get('observed_date') or snapshot_date or ''),
                      sold_quantity=integer(row.get('sold_quantity', '')),
                      status=str(row.get('status', 'observed')),
                      start_date=day(row['start_date']) if row.get('start_date') else None,
                      end_date=day(row['end_date']) if row.get('end_date') else None)
    return result


def read_report(path, kind='auto', snapshot_date=None, mapping=None):
    path = Path(path)
    if path.suffix.lower() == '.xlsx':
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True)
        try:
            matrix = list(workbook.active.iter_rows(values_only=True))
        finally:
            workbook.close()
    else:
        with path.open(encoding='utf-8-sig', newline='') as file:
            matrix = list(csv.reader(file))
    reverse = {header(alias): field for field, aliases in ALIASES.items() for alias in aliases}
    reverse.update({header(external): internal for internal, external in (mapping or {}).items()})
    for offset, cells in enumerate(matrix):
        fields = [reverse.get(header(c), '') for c in cells]
        if 'item_id' in fields and 'title' in fields:
            break
    else:
        raise ValueError(f'{path}: cannot find Item Number / Title header; set column_mapping')
    recognized = [f for f in fields if f]
    if len(set(recognized)) != len(recognized):
        raise ValueError(f'{path}: ambiguous duplicate mapped columns')
    if kind == 'auto':
        kind = 'orders' if 'order_id' in fields else 'listings'
    required = {'item_id', 'title'} | ({'order_id', 'quantity', 'sold_date'} if kind == 'orders' else {'sold_quantity'})
    if not required.issubset(fields):
        raise ValueError(f'{path}: missing columns {sorted(required - set(fields))}')
    rows = []
    for number, cells in enumerate(matrix[offset + 1:], offset + 2):
        if not any(c is not None and str(c).strip() for c in cells):
            continue
        row = {f: v if v is not None else '' for f, v in zip(fields, cells) if f}
        try:
            rows.append(normalize(row, kind, snapshot_date))
        except ValueError as exc:
            raise ValueError(f'{path.name} row {number}: {exc}') from exc
    return kind, rows


def asin_from_sku(sku):
    match = SKU_ASIN.fullmatch(sku or '')
    if not match:
        return None
    tail = match.group(1)
    try:
        decoded = base64.b64decode(tail + '=' * (-len(tail) % 4), validate=True).decode('ascii')
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded if ASIN.fullmatch(decoded) else None


def product_key(row):
    asin = asin_from_sku(row.get('sku'))
    if asin:
        return 'asin:' + asin
    return ('sku:' + row['sku']) if row.get('sku') else ('title:' + header(row['title']))


# eBay API sources are authoritative for quantities and dates. 'ebay_backfill' marks sales known
# only from the accounting history backfill (see services/sourcing/analysis.py listing_metrics).
EBAY_SOURCES = {'ebay', 'ebay_backfill'}


def save_rows(db, rows, kind, source='file'):
    """Caller owns transaction. Aggregate variations at parent listing level."""
    groups = defaultdict(list)
    for row in rows:
        observed = row['sold_date'] if kind == 'orders' else row['observed_date']
        # An ASIN identity upgrades an earlier SKU/title proxy; listing dates only widen.
        db.execute('''INSERT INTO listings(item_id,title,product_key,first_seen,start_date,end_date) VALUES (?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),
            product_key=CASE WHEN excluded.product_key LIKE 'asin:%' THEN excluded.product_key ELSE product_key END,
            start_date=coalesce(min(start_date,excluded.start_date),start_date,excluded.start_date),
            end_date=coalesce(excluded.end_date,end_date)''',
                   (row['item_id'], row['title'], product_key(row), observed, row.get('start_date'), row.get('end_date')))
        key = (row['order_id'], row['item_id']) if kind == 'orders' else (row['item_id'], observed)
        groups[key].append(row)
    for key, entries in groups.items():
        first = entries[0]
        if kind == 'orders':
            identities = [r.get('line_id') or json.dumps(r, sort_keys=True) for r in entries]
            if len(set(identities)) != len(identities):
                raise ValueError(f'Duplicate or ambiguous order lines for {key}; remove duplicate rows or provide Transaction ID')
            if len({r['sold_date'] for r in entries}) != 1:
                raise ValueError(f'Conflicting order dates for {key}')
            quantity = sum(r['quantity'] for r in entries)
            existing = db.execute('SELECT * FROM sales WHERE order_id=? AND item_id=?', key).fetchone()
            if existing and source not in EBAY_SOURCES and existing['quantity'] != quantity:
                raise ValueError(f'Conflicting quantity for order/listing {key}; use authoritative eBay sync')
            if existing and source not in EBAY_SOURCES and existing['sold_date'] != first['sold_date']:
                raise ValueError(f'Conflicting order date for {key}')
            # A backfill never downgrades a sale already known from a regular sync or import.
            db.execute('''INSERT INTO sales VALUES (?,?,?,?,?) ON CONFLICT(order_id,item_id) DO UPDATE SET
                quantity=excluded.quantity, sold_date=excluded.sold_date,
                source=CASE WHEN excluded.source='ebay_backfill' AND sales.source<>'ebay_backfill'
                            THEN sales.source ELSE excluded.source END''',
                       (*key, first['sold_date'], quantity, source))
        else:
            quantity = sum(r['sold_quantity'] for r in entries)
            db.execute('INSERT INTO snapshots VALUES (?,?,?,?) ON CONFLICT(item_id,observed_date) DO UPDATE SET sold_quantity=excluded.sold_quantity,status=excluded.status',
                       (*key, quantity, first['status']))


def import_file(db, path, kind='auto', snapshot_date=None, mapping=None):
    interpretation = json.dumps([kind, snapshot_date, mapping], sort_keys=True).encode()
    digest = hashlib.sha256(Path(path).read_bytes() + interpretation).hexdigest()
    if db.execute('SELECT 1 FROM imports WHERE digest=?', (digest,)).fetchone():
        return 0
    actual, rows = read_report(path, kind, snapshot_date, mapping)
    with db:
        save_rows(db, rows, actual)
        db.execute('INSERT INTO imports(digest,filename) VALUES (?,?)', (digest, Path(path).name))
    return len(rows)
