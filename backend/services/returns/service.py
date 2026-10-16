"""Return status per order, by precedence:
1. the user's manual override (always wins)
2. a completed eBay return (Post-Order search)
3. a full refund on the order itself (orders.payment_status FULLY_REFUNDED), which later
   return-case updates can never hide
4. the latest eBay return-case status, else NONE
The effective status feeds services/financials/calculator.py."""
import json
from datetime import datetime, timezone

from ...integrations.ebay.mapping import return_status
from ...models.statuses import RETURN_STATUSES


class ReturnStatusError(ValueError):
    pass


def stamp(moment):
    """Convert a return-event timestamp to second-resolution UTC text.

    Args:
        moment: Aware datetime to format or assign to a local chart bucket.

    Returns:
        ISO timestamp ending in Z.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def effective_status(ebay_status, manual_status, payment_status=None):
    """Resolve manual, completed-return, full-refund and eBay case statuses by precedence.

    Args:
        ebay_status: Nullable eBay-derived return status.
        manual_status: User return-status override, or None to use derived status.
        payment_status: Optional raw eBay payment status, including FULLY_REFUNDED.

    Returns:
        Effective return-status string, defaulting to NONE.
    """
    if manual_status:
        return manual_status
    if ebay_status == 'RETURN_COMPLETED':
        return ebay_status
    if payment_status == 'FULLY_REFUNDED':
        return 'REFUNDED'
    return ebay_status or 'NONE'


def set_ebay_status(db, order_id, status, return_id, raw, updated_at):
    """Upsert eBay return state while preserving absent case IDs/raw data and manual overrides. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        status: Mapped eBay return-status string to persist without altering any manual override.
        return_id: Optional eBay return-case identifier; None preserves the existing identifier.
        raw: Serialized raw return response, or None to retain previously stored raw data.
        updated_at: ISO audit timestamp to store with the return record.

    Returns:
        None; writes the return record and timestamp.
    """
    db.execute('''INSERT INTO returns(order_id, ebay_status, ebay_return_id, raw_json, updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(order_id) DO UPDATE SET ebay_status=excluded.ebay_status,
        ebay_return_id=coalesce(excluded.ebay_return_id, returns.ebay_return_id),
        raw_json=coalesce(excluded.raw_json, returns.raw_json), updated_at=excluded.updated_at''',
               (order_id, status, return_id, raw, updated_at))


def store_returns(db, members, now=None):
    """Map Post-Order search results into return records for known orders. Caller owns the transaction.

    Apply Post-Order return search results to known orders. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        members: Raw eBay Post-Order return-search result members.
        now: Optional audit timestamp; None uses current UTC time for each applied result.

    Returns:
        Number of return members applied; unknown orders are ignored.
    """
    count = 0
    for member in members:
        order_id = member.get('orderId')
        if not order_id or not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
            continue
        set_ebay_status(db, order_id, return_status(member), member.get('returnId'),
                        json.dumps(member, sort_keys=True), stamp(now or datetime.now(timezone.utc)))
        count += 1
    return count


def set_manual_status(db, order_id, status, now):
    """Set a validated manual return override or clear it to restore derived state. Caller owns the transaction.

    Manual override of the eBay-derived return status (None restores the eBay value).

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        status: Valid manual return status, or None to clear the override.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        None; raises ReturnStatusError for unknown orders or statuses.
    """
    if not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
        raise ReturnStatusError(f'Unknown order {order_id}')
    if status is not None and status not in RETURN_STATUSES:
        raise ReturnStatusError(f'Unknown return status {status}')
    db.execute('''INSERT INTO returns(order_id, manual_status, updated_at) VALUES (?,?,?)
                  ON CONFLICT(order_id) DO UPDATE SET manual_status=excluded.manual_status,
                  updated_at=excluded.updated_at''', (order_id, status, stamp(now)))
