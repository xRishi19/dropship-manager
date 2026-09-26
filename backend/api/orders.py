"""Orders table, order detail, manual Amazon match actions, return override and tax export."""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response

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
    return request.app.state.config.accounting.ordering_fee_cents


def filtered(request, db, range, status, return_status, match_status, q):
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
    rows = filtered(request, db, range, status, return_status, match_status, q)
    return {'total': len(rows), 'orders': [{k: r.get(k) for k in SUMMARY} for r in rows[offset:offset + limit]]}


@router.get('/orders/export')
def export(request: Request, scope: str = 'filtered', range: str = '30d', status: str | None = None,
           return_status: str | None = None, match_status: str | None = None, q: str | None = None,
           db=Depends(get_db)):
    rows = load_orders(db, ordering_fee_cents=fee(request)) if scope == 'all' else filtered(request, db, range, status, return_status, match_status, q)
    rows.sort(key=lambda r: r['created_at'])
    name = 'orders-all.csv' if scope == 'all' else f'orders-{range}.csv'
    return Response(export_csv(rows), media_type='text/csv',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


def one(db, order_id, ordering_fee_cents=0):
    rows = load_orders(db, order_ids=[order_id], ordering_fee_cents=ordering_fee_cents)
    if not rows:
        raise HTTPException(404, 'Order not found')
    return rows[0]


@router.get('/orders/{order_id}')
def order_detail(order_id: str, request: Request, db=Depends(get_db)):
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
    rows = db.execute(f'''SELECT e.gmail_message_id, e.received_at, e.subject, e.first_name, e.city, e.state,
            e.quantity, e.grand_total_cents, e.parse_status, e.parse_error, m.order_id AS attached_to
            FROM amazon_email_purchases e LEFT JOIN order_matches m ON m.gmail_message_id=e.gmail_message_id
            WHERE {where} ORDER BY e.received_at''', params)
    return {r['gmail_message_id']: dict(r) for r in rows}


@router.get('/orders/{order_id}/emails')
def nearby_emails(order_id: str, db=Depends(get_db)):
    """Amazon emails from 1 day before to 3 days after the order, for manual linking."""
    created = parse_time(one(db, order_id)['created_at'])
    stamp = lambda d: d.isoformat(timespec='seconds').replace('+00:00', 'Z')
    rows = email_rows(db, 'e.received_at BETWEEN ? AND ?',
                      [stamp(created - timedelta(days=1)), stamp(created + timedelta(days=3))])
    return list(rows.values())


def act(db, action):
    try:
        with db:
            action()
    except manual.MatchError as exc:
        raise HTTPException(409 if 'already attached' in str(exc) else 400, str(exc)) from None


@router.post('/orders/{order_id}/amazon-cost')
def set_cost(order_id: str, body: CostBody, request: Request, db=Depends(get_db)):
    cents = None
    if body.amount not in (None, ''):
        try:
            cents = int((Decimal(body.amount.replace('$', '').replace(',', '')) * 100).to_integral_value())
        except InvalidOperation:
            raise HTTPException(400, 'Amount must be a dollar value like 23.45') from None
    act(db, lambda: manual.set_manual_cost(db, order_id, cents, now()))
    return order_detail(order_id, request, db)


@router.post('/orders/{order_id}/match')
def link(order_id: str, body: LinkBody, request: Request, db=Depends(get_db)):
    act(db, lambda: manual.link_email(db, order_id, body.gmail_message_id, now(), body.replace))
    return order_detail(order_id, request, db)


@router.delete('/orders/{order_id}/match')
def unlink(order_id: str, request: Request, db=Depends(get_db)):
    act(db, lambda: manual.unlink(db, order_id, now()))
    return order_detail(order_id, request, db)


@router.post('/orders/{order_id}/recheck')
def recheck(order_id: str, request: Request, db=Depends(get_db)):
    one(db, order_id)
    with db:
        run_matching(db, request.app.state.config.matching, now(), order_ids=[order_id])
    return order_detail(order_id, request, db)


@router.put('/orders/{order_id}/return-status')
def return_status(order_id: str, body: ReturnBody, request: Request, db=Depends(get_db)):
    act(db, lambda: manual.set_return_status(db, order_id, body.status, now()))
    return order_detail(order_id, request, db)
