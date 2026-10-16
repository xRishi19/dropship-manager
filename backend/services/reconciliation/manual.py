"""User actions on Amazon matches and return status. Manual decisions are never overwritten
by the automatic matcher or by eBay sync."""
import json

from ..returns.service import ReturnStatusError, set_manual_status
from .matcher import now_iso


class MatchError(ValueError):
    pass


def ensure_order(db, order_id):
    """Validate that a manual-action target exists before changing its reconciliation data.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.

    Returns:
        None; raises MatchError for an unknown order.
    """
    if not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
        raise MatchError(f'Unknown order {order_id}')


def current(db, order_id):
    """Read the current reconciliation row for an order.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.

    Returns:
        Match dictionary, or None when the order has not been evaluated.
    """
    row = db.execute('SELECT * FROM order_matches WHERE order_id=?', (order_id,)).fetchone()
    return dict(row) if row else None


def upsert_match(db, order_id, now, **fields):
    """Merge supplied match fields with existing values and persist the updated match. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        now: Reference aware datetime used for timing and persisted audit state.
        fields: Keyword match fields to merge into the existing reconciliation row.

    Returns:
        None; creates or updates order_matches and its updated_at timestamp.
    """
    existing = current(db, order_id) or {'order_id': order_id, 'status': 'PENDING'}
    existing.update(fields, updated_at=now_iso(now))
    columns = ['order_id', 'status', 'gmail_message_id', 'confidence', 'candidates_json', 'manual_cost_cents',
               'method', 'rejected_json', 'matched_at', 'updated_at']
    db.execute(f'''INSERT INTO order_matches({",".join(columns)}) VALUES ({",".join("?" * len(columns))})
                   ON CONFLICT(order_id) DO UPDATE SET {", ".join(f"{c}=excluded.{c}" for c in columns[1:])}''',
               [existing.get(c) for c in columns])


def set_manual_cost(db, order_id, cost_cents, now):
    """Set or clear a manually confirmed Amazon cost and update the match method/status. Caller owns the transaction.

    Enter (or clear with None) the Amazon cost by hand.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        cost_cents: Manual Amazon cost in cents; None clears the override.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        None; raises MatchError for unknown orders or negative costs.
    """
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
    """Attach an Amazon confirmation manually, optionally moving it from its current holder. Caller owns the transaction.

    Attach a specific Amazon email. If another order holds it, `replace` moves it here.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        gmail_message_id: Identifier of the stored Amazon confirmation to attach.
        now: Reference aware datetime used for timing and persisted audit state.
        replace: Whether an email already held by another order may be moved to this order.

    Returns:
        None; raises MatchError for missing records or an occupied email without replacement permission.
    """
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
    """Release an attached email and remember its rejection so it is not automatically offered again. Caller owns the transaction.

    Detach the email (it becomes available to other orders and is never re-offered to this one).

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        None; retains a manual-cost match when present and otherwise restores PENDING.
    """
    ensure_order(db, order_id)
    match = current(db, order_id)
    if not match or not match.get('gmail_message_id'):
        raise MatchError('No Amazon email is attached to this order')
    rejected = set(json.loads(match.get('rejected_json') or '[]')) | {match['gmail_message_id']}
    upsert_match(db, order_id, now, gmail_message_id=None, confidence=None, matched_at=None,
                 status='MANUAL' if match.get('manual_cost_cents') is not None else 'PENDING',
                 method='manual_unlink', rejected_json=json.dumps(sorted(rejected)))


def set_return_status(db, order_id, status, now):
    """Apply a user return override through the returns service. Caller owns the transaction.

    Manual return-status override (delegates to the returns service).

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        status: Valid manual return-status override, or None to clear it.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        None; translates return validation failures into MatchError.
    """
    try:
        set_manual_status(db, order_id, status, now)
    except ReturnStatusError as exc:
        raise MatchError(str(exc)) from None
