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
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def effective_status(ebay_status, manual_status, payment_status=None):
    if manual_status:
        return manual_status
    if ebay_status == 'RETURN_COMPLETED':
        return ebay_status
    if payment_status == 'FULLY_REFUNDED':
        return 'REFUNDED'
    return ebay_status or 'NONE'


def set_ebay_status(db, order_id, status, return_id, raw, updated_at):
    db.execute('''INSERT INTO returns(order_id, ebay_status, ebay_return_id, raw_json, updated_at) VALUES (?,?,?,?,?)
        ON CONFLICT(order_id) DO UPDATE SET ebay_status=excluded.ebay_status,
        ebay_return_id=coalesce(excluded.ebay_return_id, returns.ebay_return_id),
        raw_json=coalesce(excluded.raw_json, returns.raw_json), updated_at=excluded.updated_at''',
               (order_id, status, return_id, raw, updated_at))


def store_returns(db, members, now=None):
    """Apply Post-Order return search results to known orders. Caller owns the transaction."""
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
    """Manual override of the eBay-derived return status (None restores the eBay value)."""
    if not db.execute('SELECT 1 FROM orders WHERE order_id=?', (order_id,)).fetchone():
        raise ReturnStatusError(f'Unknown order {order_id}')
    if status is not None and status not in RETURN_STATUSES:
        raise ReturnStatusError(f'Unknown return status {status}')
    db.execute('''INSERT INTO returns(order_id, manual_status, updated_at) VALUES (?,?,?)
                  ON CONFLICT(order_id) DO UPDATE SET manual_status=excluded.manual_status,
                  updated_at=excluded.updated_at''', (order_id, status, stamp(now)))
