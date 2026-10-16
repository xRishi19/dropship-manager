"""Dashboard time ranges, bucketing (in the local time zone) and summary cards."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

RANGES = ('1d', '1w', '30d', 'month', '3m', '12m', 'ytd')


def local_zone(name=None):
    """Resolve the configured dashboard time zone, falling back to the system zone and then UTC.

    Configured IANA zone, else this Mac's zone (from /etc/localtime), else UTC.

    Args:
        name: Optional IANA timezone name; absent values use the system-zone/UTC fallback.

    Returns:
        ZoneInfo or timezone object; an invalid explicitly configured name raises an error.
    """
    if name:
        return ZoneInfo(name)
    target = str(Path('/etc/localtime').resolve())
    if 'zoneinfo/' in target:
        try:
            return ZoneInfo(target.split('zoneinfo/', 1)[1])
        except Exception:
            pass
    return timezone.utc


def month_start(day, months_back=0):
    """Find the first day of a month with a relative backward month offset.

    Args:
        day: Reference calendar date for relative month arithmetic.
        months_back: Number of months to move backward; negative values move forward.

    Returns:
        Date at the first day of the requested month, including year rollover.
    """
    month = day.month - 1 - months_back
    return date(day.year + month // 12, month % 12 + 1, 1)


def time_range(range_name, now, zone):
    """Resolve a dashboard preset or calendar month into local-time bounds and bucket starts.

    (start, end, granularity, bucket starts) as aware local datetimes; end is exclusive.

    Args:
        range_name: Dashboard range preset or ISO YYYY-MM calendar month.
        now: Reference aware datetime used for timing and persisted audit state.
        zone: Accounting time-zone object used for local calendar boundaries.

    Returns:
        Tuple (start, exclusive end, granularity, buckets); hourly buckets respect DST transitions.
    """
    local = now.astimezone(zone)
    today = local.date()
    midnight = lambda d: datetime(d.year, d.month, d.day, tzinfo=zone)
    if range_name == '1d':  # Today, local midnight to next midnight; UTC steps give 23/25 hours on DST days.
        start = midnight(today).astimezone(timezone.utc)
        end = midnight(today + timedelta(days=1)).astimezone(timezone.utc)
        hours = (end - start) // timedelta(hours=1)
        buckets = [(start + timedelta(hours=h)).astimezone(zone) for h in range(hours)]
        return buckets[0], end.astimezone(zone), 'hour', buckets
    if range_name in ('1w', '30d', 'month'):
        first = {'1w': today - timedelta(days=6), '30d': today - timedelta(days=29), 'month': today.replace(day=1)}[range_name]
        days = [first + timedelta(days=d) for d in range((today - first).days + 1)]
        return midnight(days[0]), midnight(today + timedelta(days=1)), 'day', [midnight(d) for d in days]
    if range_name == '3m':  # 13 Monday-starting weeks is readable; 90 daily bars is not.
        monday = today - timedelta(days=today.weekday())
        weeks = [monday - timedelta(weeks=w) for w in range(12, -1, -1)]
        return midnight(weeks[0]), midnight(today + timedelta(days=1)), 'week', [midnight(w) for w in weeks]
    if range_name in ('12m', 'ytd'):
        count = 12 if range_name == '12m' else today.month
        months = [month_start(today, m) for m in range(count - 1, -1, -1)]
        return midnight(months[0]), midnight(today + timedelta(days=1)), 'month', [midnight(m) for m in months]
    if isinstance(range_name, str) and len(range_name) == 7 and range_name[4] == '-':  # 'YYYY-MM': that whole local month.
        first = date(int(range_name[:4]), int(range_name[5:]), 1)
        after = month_start(first, -1)  # Days after today are empty buckets.
        days = [first + timedelta(days=d) for d in range((after - first).days)]
        return midnight(first), midnight(after), 'day', [midnight(d) for d in days]
    raise ValueError(f'Unknown range {range_name!r}; use one of {RANGES} or YYYY-MM')


def bucket_of(moment, granularity, zone):
    """Floor a timestamp to the chart bucket boundary in the dashboard zone.

    Args:
        moment: Aware datetime to format or assign to a local chart bucket.
        granularity: Chart grouping unit: hour, day, week or month.
        zone: Accounting time-zone object used for local calendar boundaries.

    Returns:
        Aware bucket-start datetime; hourly buckets distinguish repeated DST hours.
    """
    if granularity == 'hour':
        return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).astimezone(zone)
    local = moment.astimezone(zone)
    day = local.date()
    if granularity == 'week':
        day -= timedelta(days=day.weekday())
    elif granularity == 'month':
        day = day.replace(day=1)
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def parse_utc(value):
    """Parse an ISO timestamp including the Z UTC designator.

    Args:
        value: ISO timestamp string; a trailing Z is accepted as UTC.

    Returns:
        Datetime with the timestamp's supplied timezone offset.
    """
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def utc_iso(moment):
    """Normalize an aware timestamp to a second-resolution UTC representation.

    Args:
        moment: Aware datetime to format or assign to a local chart bucket.

    Returns:
        ISO UTC timestamp string ending in Z.
    """
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def label(bucket, granularity):
    """Format a readable chart label for an hourly, daily, weekly or monthly bucket.

    Args:
        bucket: Aware bucket-start datetime; fold marks a repeated local DST hour.
        granularity: Chart grouping unit: hour, day, week or month.

    Returns:
        Label string; repeated DST hours include their zone abbreviation.
    """
    if granularity == 'hour':  # The repeated hour on the DST fall-back day gets its zone name.
        return bucket.strftime('%b %d %H:00 %Z') if bucket.fold else bucket.strftime('%b %d %H:00')
    if granularity == 'month':
        return bucket.strftime('%b %Y')
    return bucket.strftime('%b %d') if granularity == 'day' else 'Week of ' + bucket.strftime('%b %d')


def summarize(orders, cashback_rate=0.0):
    """Aggregate dashboard cards using effective order financials and separately reported cashback.

    Cards. Excluded (cancelled/returned/refunded) orders count nowhere in the order count, revenue
    or cost, but their ordering-provider fee (-fee effective profit) still reduces profit; orders with
    unknown Amazon cost count in orders and revenue but not in cost or profit (nor their fee, yet).
    Prime Visa cashback is a share of that same Amazon cost, shown separately and never added to profit.
    Average profit and margin use costed included orders only (their profit includes the fee).

    Args:
        orders: Enriched ledger order dictionaries with effective financials, exclusion flags and match status.
        cashback_rate: Cashback fraction applied only to included confirmed Amazon costs; defaults to zero.

    Returns:
        Dictionary of monetary totals, order/review counts, exclusions, average profit and margin.
    """
    included = [o for o in orders if not o['excluded']]
    excluded = [o for o in orders if o['excluded']]
    costed = [o for o in included if o['cost_known'] and o['effective_revenue_cents'] is not None]
    revenue = sum(o['effective_revenue_cents'] or 0 for o in included)
    cost = sum(o['effective_cost_cents'] for o in costed)
    costed_profit = sum(o['effective_profit_cents'] for o in costed)
    profit = costed_profit + sum(o['effective_profit_cents'] for o in excluded)
    fees = sum(o.get('ordering_fee_cents', 0) for o in costed + excluded)
    costed_revenue = sum(o['effective_revenue_cents'] for o in costed)
    return {'total_orders': len(included), 'net_revenue_cents': revenue, 'amazon_cost_cents': cost,
            'profit_cents': profit, 'ordering_fees_cents': fees,
            'prime_cashback_cents': round(cost * cashback_rate), 'prime_cashback_rate': cashback_rate,
            'avg_profit_per_order_cents': round(costed_profit / len(costed)) if costed else None,
            'profit_margin': round(costed_profit / costed_revenue, 4) if costed_revenue else None,
            'awaiting_cost': len(included) - len(costed),
            'needs_review': sum(o['match_status'] == 'NEEDS_REVIEW' for o in included),
            'revenue_estimated': sum(o['revenue_estimated'] for o in included),
            'excluded': {'cancelled': sum(o['display_status'] == 'Cancelled' for o in orders),
                         'returned': sum(o['display_status'] == 'Returned' for o in orders),
                         'refunded': sum(o['display_status'] == 'Refunded' for o in orders)},
            'return_pending': sum('RETURN_PENDING' in o['flags'] for o in included)}


def series(orders, buckets, granularity, zone):
    # Keyed by UTC instant: on the DST fall-back day the two local 01:00 buckets compare equal as
    # same-zone datetimes, and would otherwise merge and double-count.
    """Aggregate order financials into predefined chart buckets, keeping DST hours distinct.

    Args:
        orders: Enriched ledger order dictionaries with created_at and effective financials.
        buckets: Ordered aware bucket-start datetimes defining the output chart.
        granularity: Chart grouping unit: hour, day, week or month.
        zone: Accounting time-zone object used for local calendar boundaries.

    Returns:
        List of chart-point dictionaries in supplied bucket order, with UTC keys, labels and totals.
    """
    points = {utc_iso(b): {'revenue_cents': 0, 'cost_cents': 0, 'profit_cents': 0, 'orders': 0, 'awaiting_cost': 0}
              for b in buckets}
    for order in orders:
        point = points.get(utc_iso(bucket_of(parse_utc(order['created_at']), granularity, zone)))
        if point is None:
            continue
        if order['excluded']:  # Not an order or revenue, but its ordering-provider fee (-fee) still counts.
            point['profit_cents'] += order['effective_profit_cents']
            continue
        point['orders'] += 1
        point['revenue_cents'] += order['effective_revenue_cents'] or 0
        if order['cost_known'] and order['effective_revenue_cents'] is not None:
            point['cost_cents'] += order['effective_cost_cents']
            point['profit_cents'] += order['effective_profit_cents']
        else:
            point['awaiting_cost'] += 1  # In revenue, not yet in cost/profit.
    month = (lambda b: {'month': b.strftime('%Y-%m')}) if granularity == 'month' else (lambda b: {})  # Local year-month.
    return [{'bucket': utc_iso(b), 'label': label(b, granularity), **month(b), **points[utc_iso(b)]} for b in buckets]
