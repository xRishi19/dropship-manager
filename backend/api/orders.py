"""Orders table, order detail, manual Amazon match actions, return override and tax export."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from fastapi import BackgroundTasks, APIRouter, Depends, HTTPException, Request, Response

from ..models.schemas import CostBody, LinkBody, ReturnBody
from ..services.financials.export import export_csv
from ..services.financials.ledger import load_orders
from ..services.reconciliation import manual
from ..services.reconciliation.matcher import run_matching
from ..services.reconciliation.scoring import parse_time
from .deps import get_db, now, resolve_range

router = APIRouter(tags=['orders'])
EBAY_ITEM_URL = 'https://www.ebay.com/itm/{}'
EBAY_ORDER_URL = 'https://www.ebay.com/sh/ord/details?orderid={}'
HIDDEN = {'raw_json', 'candidates_json', 'other_fees_json', 'rejected_json'}
SUMMARY = ['order_id', 'created_at', 'product_title', 'quantity', 'display_status', 'return_status', 'match_status',
           'match_confidence', 'flags', 'excluded', 'cost_known', 'revenue_estimated', 'net_revenue_cents',
           'amazon_cost_cents', 'original_profit_cents', 'effective_profit_cents', 'effective_revenue_cents',
           'effective_cost_cents', 'margin', 'roi', 'ordering_fee_cents', 'category', 'asin']


def fee(request):
    """Read the configured per-order ordering-provider fee for ledger calculations.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.

    Returns:
        Integer fee in cents.
    """
    return request.app.state.config.accounting.ordering_fee_cents


def filtered(request, db, range, status, return_status, match_status, q):
    """Load enriched orders and apply date, display/return/match status and text-search filters.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.
        range: Dashboard preset, month or all-time selector passed to resolve_range.
        status: Optional case-insensitive display-status filter; empty/None leaves all display statuses.
        return_status: Optional exact effective return-status filter; empty/None leaves all return statuses.
        match_status: Optional comma-separated match statuses; COST_UNKNOWN also includes orders without known cost.
        q: Optional case-insensitive search text for item/order identifiers, title or buyer name.

    Returns:
        List of matching order dictionaries in newest-first ledger order.
    """
    start, end, *_ = resolve_range(request, range)
    rows = load_orders(db, start, end, ordering_fee_cents=fee(request))
    if status:
        rows = [r for r in rows if r['display_status'].lower() == status.lower()]
    if return_status:
        rows = [r for r in rows if r['return_status'] == return_status]
    if match_status:
        wanted = set(match_status.split(','))
        rows = [r for r in rows if r['match_status'] in wanted or ('COST_UNKNOWN' in wanted and not r['cost_known'])]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in r['product_title'].lower() or needle in r['order_id'].lower()
                or any(needle in i['item_id'] for i in r['items']) or needle in (r.get('ship_name') or '').lower()]
    return rows


@router.get('/orders')
def list_orders(request: Request, range: str = '30d', status: str | None = None, return_status: str | None = None,
                match_status: str | None = None, q: str | None = None, limit: int = 100, offset: int = 0,
                db=Depends(get_db)):
    """Serve a paginated projection of the filtered orders table.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        range: Dashboard preset, month or all-time selector; defaults to 30d.
        status: Optional case-insensitive display-status filter; empty/None leaves all display statuses.
        return_status: Optional exact effective return-status filter; empty/None leaves all return statuses.
        match_status: Optional comma-separated match statuses; COST_UNKNOWN also includes orders without known cost.
        q: Optional case-insensitive search text for item/order identifiers, title or buyer name.
        limit: Maximum summary rows returned per page; defaults to 100.
        offset: Number of filtered rows to skip; defaults to zero.
        db: Open SQLite connection used for the query or write.

    Returns:
        Dictionary with total matching count and an orders list containing summary fields.
    """
    rows = filtered(request, db, range, status, return_status, match_status, q)
    return {'total': len(rows), 'orders': [{k: r.get(k) for k in SUMMARY} for r in rows[offset:offset + limit]]}


@router.get('/orders/export')
def export(request: Request, scope: str = 'filtered', range: str = '30d', status: str | None = None,
           return_status: str | None = None, match_status: str | None = None, q: str | None = None,
           db=Depends(get_db)):
    """Render selected or all orders as an oldest-first accounting CSV download.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        scope: Export scope; all bypasses filters and the default filtered uses the current selection.
        range: Dashboard preset, month or all-time selector for filtered exports; defaults to 30d.
        status: Optional case-insensitive display-status filter; empty/None leaves all display statuses.
        return_status: Optional exact effective return-status filter; empty/None leaves all return statuses.
        match_status: Optional comma-separated match statuses; COST_UNKNOWN also includes orders without known cost.
        q: Optional case-insensitive search text for item/order identifiers, title or buyer name.
        db: Open SQLite connection used for the query or write.

    Returns:
        HTTP Response with CSV content and attachment filename.
    """
    rows = load_orders(db, ordering_fee_cents=fee(request)) if scope == 'all' else filtered(request, db, range, status, return_status, match_status, q)
    rows.sort(key=lambda r: r['created_at'])
    name = 'orders-all.csv' if scope == 'all' else f'orders-{range}.csv'
    return Response(export_csv(rows), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


def one(db, order_id, ordering_fee_cents=0):
    """Load one order with calculated financials or report a missing target.

    Args:
        db: Open SQLite connection used for the query or write.
        order_id: eBay order identifier to read or modify.
        ordering_fee_cents: Per-order ordering-provider fee in integer cents, deducted from profit.

    Returns:
        Enriched order dictionary; unknown identifiers raise HTTPException 404.
    """
    rows = load_orders(db, order_ids=[order_id], ordering_fee_cents=ordering_fee_cents)
    if not rows:
        raise HTTPException(404, 'Order not found')
    return rows[0]


@router.get('/orders/{order_id}')
def order_detail(order_id: str, request: Request, db=Depends(get_db)):
    """Build order detail with eBay links and readable matched/candidate email information.

    Args:
        order_id: eBay order identifier to read or modify.
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Detail dictionary excluding raw/internal JSON fields.
    """
    order = one(db, order_id, fee(request))
    detail = {k: v for k, v in order.items() if k not in HIDDEN}
    detail['ebay_order_url'] = EBAY_ORDER_URL.format(order_id)
    for item in detail['items']:
        item['ebay_item_url'] = EBAY_ITEM_URL.format(item['item_id'])
    ids = [c['gmail_message_id'] for c in order['candidates']] + ([order['gmail_message_id']] if order.get('gmail_message_id') else [])
    emails = email_rows(db, 'e.gmail_message_id IN ({})'.format(','.join('?' * len(ids))), ids) if ids else {}
    detail['candidates'] = [{**c, 'email': emails.get(c['gmail_message_id'])} for c in order['candidates']]
    detail['matched_email'] = emails.get(order.get('gmail_message_id'))
    return detail


def email_rows(db, where, params):
    """Load selected confirmation-email facts with the order currently holding each email.

    Args:
        db: Open SQLite connection used for the query or write.
        where: Trusted SQL predicate selecting confirmation rows; values belong in params.
        params: Bound values for placeholders in the trusted SQL predicate.

    Returns:
        Mapping of Gmail message ID to email dictionary.
    """
    rows = db.execute(f'''SELECT e.gmail_message_id, e.received_at, e.subject, e.first_name, e.city, e.state,
            e.quantity, e.grand_total_cents, e.parse_status, e.parse_error, m.order_id AS attached_to
            FROM amazon_email_purchases e LEFT JOIN order_matches m ON m.gmail_message_id=e.gmail_message_id
            WHERE {where} ORDER BY e.received_at''', params)
    return {r['gmail_message_id']: dict(r) for r in rows}


@router.get('/orders/{order_id}/emails')
def nearby_emails(order_id: str, db=Depends(get_db)):
    """Find confirmations from one day before through three days after an order for manual linking.

    Amazon emails from 1 day before to 3 days after the order, for manual linking.

    Args:
        order_id: eBay order identifier to read or modify.
        db: Open SQLite connection used for the query or write.

    Returns:
        Chronologically ordered list of email dictionaries, including current attachment information.
    """
    created = parse_time(one(db, order_id)['created_at'])
    stamp = lambda d: d.isoformat(timespec='seconds').replace('+00:00', 'Z')
    rows = email_rows(db, 'e.received_at BETWEEN ? AND ?',
                      [stamp(created - timedelta(days=1)), stamp(created + timedelta(days=3))])
    return list(rows.values())


def act(db, action):
    """Commit a manual action atomically and translate match validation failures into HTTP errors.

    Args:
        db: Open SQLite connection used for the query or write.
        action: Zero-argument callable implementing the manual database action.

    Returns:
        None; raises HTTPException 409 for occupied emails and 400 for other MatchError failures.
    """
    try:
        with db:
            action()
    except manual.MatchError as exc:
        raise HTTPException(409 if 'already attached' in str(exc) else 400, str(exc)) from None


@router.post('/orders/{order_id}/amazon-cost')
def set_cost(order_id: str, body: CostBody, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    """Convert an entered dollar cost, commit its manual match state and schedule notification eligibility checks.

    Args:
        order_id: eBay order identifier to read or modify.
        body: CostBody with optional dollar amount text; None or empty text clears the manual cost.
        request: FastAPI request providing application configuration and shared runtime services.
        background_tasks: FastAPI task collector used to wake notifications after the response.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated order detail dictionary; empty input clears the manual cost.
    """
    cents = None
    if body.amount not in (None, ''):
        try:
            cents = int((Decimal(body.amount.replace('$', '').replace(',', '')) * 100).to_integral_value())
        except InvalidOperation:
            raise HTTPException(400, 'Amount must be a dollar value like 23.45') from None
    act(db, lambda: manual.set_manual_cost(db, order_id, cents, now()))
    background_tasks.add_task(request.app.state.notifications.trigger)
    return order_detail(order_id, request, db)


@router.post('/orders/{order_id}/match')
def link(order_id: str, body: LinkBody, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    """Commit an explicitly selected Amazon email match and schedule notifications after the response.

    Args:
        order_id: eBay order identifier to read or modify.
        body: LinkBody with the Gmail message ID and whether to replace another order's link.
        request: FastAPI request providing application configuration and shared runtime services.
        background_tasks: FastAPI task collector used to wake notifications after the response.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated order detail dictionary.
    """
    act(db, lambda: manual.link_email(db, order_id, body.gmail_message_id, now(), body.replace))
    background_tasks.add_task(request.app.state.notifications.trigger)
    return order_detail(order_id, request, db)


@router.delete('/orders/{order_id}/match')
def unlink(order_id: str, request: Request, db=Depends(get_db)):
    """Commit removal of an Amazon email match while preserving any manual cost.

    Args:
        order_id: eBay order identifier to read or modify.
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated order detail dictionary.
    """
    act(db, lambda: manual.unlink(db, order_id, now()))
    return order_detail(order_id, request, db)


@router.post('/orders/{order_id}/recheck')
def recheck(order_id: str, request: Request, background_tasks: BackgroundTasks, db=Depends(get_db)):
    """Commit a fresh automatic evaluation of one order and schedule sale notification checks.

    Args:
        order_id: eBay order identifier to read or modify.
        request: FastAPI request providing application configuration and shared runtime services.
        background_tasks: FastAPI task collector used to wake notifications after the response.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated order detail dictionary; confirmed matches remain unchanged.
    """
    one(db, order_id)
    with db:
        run_matching(db, request.app.state.config.matching, now(), order_ids=[order_id])
    background_tasks.add_task(request.app.state.notifications.trigger)
    return order_detail(order_id, request, db)


@router.put('/orders/{order_id}/return-status')
def return_status(order_id: str, body: ReturnBody, request: Request, db=Depends(get_db)):
    """Commit a user return-status override or clear it to restore derived status.

    Args:
        order_id: eBay order identifier to read or modify.
        body: ReturnBody containing a valid return-status override or None to clear it.
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated order detail dictionary.
    """
    act(db, lambda: manual.set_return_status(db, order_id, body.status, now()))
    return order_detail(order_id, request, db)
