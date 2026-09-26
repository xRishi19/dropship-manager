"""User actions on Amazon matches and return status. Manual decisions are never overwritten
by the automatic matcher or by eBay sync."""
import json

from ..returns.service import ReturnStatusError, set_manual_status
from .matcher import now_iso


class MatchError(ValueError):
    pass


def ensure_order(db, order_id):
    if not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
        raise MatchError(f'Unknown order {order_id}')


def current(db, order_id):
    row = db.execute('SELECT * FROM order_matches WHERE order_id=?', (order_id,)).fetchone()
    return dict(row) if row else None


def upsert_match(db, order_id, now, **fields):
    existing = current(db, order_id) or {'order_id': order_id, 'status': 'PENDING'}
    existing.update(fields, updated_at=now_iso(now))
    columns = ['order_id', 'status', 'gmail_message_id', 'confidence', 'candidates_json', 'manual_cost_cents',
               'method', 'rejected_json', 'matched_at', 'updated_at']
    db.execute(f'''INSERT INTO order_matches({",".join(columns)}) VALUES ({",".join("?" * len(columns))})
                   ON CONFLICT(order_id) DO UPDATE SET {", ".join(f"{c}=excluded.{c}" for c in columns[1:])}''',
               [existing.get(c) for c in columns])


def set_manual_cost(db, order_id, cost_cents, now):
    """Enter (or clear with None) the Amazon cost by hand."""
    ensure_order(db, order_id)
    if cost_cents is not None and cost_cents < 0:
        raise MatchError('Amazon cost cannot be negative')
    match = current(db, order_id) or {}
    if cost_cents is not None:
        upsert_match(db, order_id, now, manual_cost_cents=cost_cents, status='MANUAL', method='manual_cost')
    else:
        linked = match.get('gmail_message_id')
        upsert_match(db, order_id, now, manual_cost_cents=None, status='MANUAL' if linked else 'PENDING',
                     method='manual_link' if linked else 'manual_clear')


def link_email(db, order_id, gmail_message_id, now, replace=False):
    """Attach a specific Amazon email. If another order holds it, `replace` moves it here."""
    ensure_order(db, order_id)
    if not db.execute('SELECT 1 FROM amazon_email_purchases WHERE gmail_message_id=?', (gmail_message_id,)).fetchone():
        raise MatchError('Unknown Amazon email')
    holder = db.execute('SELECT order_id FROM order_matches WHERE gmail_message_id=?', (gmail_message_id,)).fetchone()
    if holder and holder['order_id'] != order_id:
        if not replace:
            raise MatchError(f'That email is already attached to order {holder["order_id"]}')
        unlink(db, holder['order_id'], now)
    upsert_match(db, order_id, now, gmail_message_id=gmail_message_id, status='MANUAL', method='manual_link',
                 matched_at=now_iso(now))


def unlink(db, order_id, now):
    """Detach the email (it becomes available to other orders and is never re-offered to this one)."""
    ensure_order(db, order_id)
    match = current(db, order_id)
    if not match or not match.get('gmail_message_id'):
        raise MatchError('No Amazon email is attached to this order')
    rejected = set(json.loads(match.get('rejected_json') or '[]')) | {match['gmail_message_id']}
    upsert_match(db, order_id, now, gmail_message_id=None, confidence=None, matched_at=None,
                 status='MANUAL' if match.get('manual_cost_cents') is not None else 'PENDING',
                 method='manual_unlink', rejected_json=json.dumps(sorted(rejected)))


def set_return_status(db, order_id, status, now):
    """Manual return-status override (delegates to the returns service)."""
    try:
        set_manual_status(db, order_id, status, now)
    except ReturnStatusError as exc:
        raise MatchError(str(exc)) from None
