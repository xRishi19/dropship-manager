"""Dashboard cards and the grouped revenue/cost/profit series."""
from datetime import timedelta

from fastapi import APIRouter, Depends, Request

from ..services.expenses.service import expenses_in_range
from ..services.financials.aggregation import parse_utc, series, summarize
from ..services.financials.ledger import load_orders
from .deps import get_db, now, resolve_range

router = APIRouter(tags=['overview'])


def local_dates(start, end, zone):
    """[start, end) as local dates, like order buckets, never past today: charges that haven't
    happened yet (e.g. later this month) don't reduce profit, so "Current Month" and picking the
    current month from the month picker agree."""
    tomorrow = now().astimezone(zone).date() + timedelta(days=1)
    first = parse_utc(start).astimezone(zone).date() if start else None
    after = min(parse_utc(end).astimezone(zone).date(), tomorrow) if end else tomorrow
    return first, after


@router.get('/overview')
def overview(request: Request, range: str = '30d', db=Depends(get_db)):
    start, end, granularity, buckets, zone = resolve_range(request, range)
    accounting = request.app.state.config.accounting
    orders = load_orders(db, start, end, ordering_fee_cents=accounting.ordering_fee_cents)
    cards = summarize(orders, accounting.prime_cashback_rate)
    # Expenses only change the Profit card's headline; profit_cents, margin and the series stay per-order.
    cards['expenses_cents'] = expenses_in_range(db, *local_dates(start, end, zone))
    cards['profit_after_expenses_cents'] = cards['profit_cents'] - cards['expenses_cents']
    return {'range': range, 'granularity': granularity, 'start': start, 'end': end, 'timezone': str(zone),
            'cards': cards, 'series': series(orders, buckets, granularity, zone) if buckets else []}
