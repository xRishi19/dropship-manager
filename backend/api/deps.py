"""Shared request dependencies."""
import re
from datetime import datetime, timezone

from fastapi import HTTPException, Request

from ..db.connection import connect
from ..services.financials.aggregation import RANGES, local_zone, time_range, utc_iso

MONTH = re.compile(r'\d{4}-\d{2}')


def get_db(request: Request):
    db = connect(request.app.state.config.database, migrate=False)
    try:
        yield db
    finally:
        db.close()


def now():
    return datetime.now(timezone.utc)


def resolve_range(request, range_name):
    """(start_utc, end_utc, granularity, buckets, zone) for a dashboard range; 'all' = no bounds."""
    zone = local_zone(request.app.state.config.accounting.timezone)
    if range_name in (None, '', 'all'):
        return None, None, None, None, zone
    current = now()
    if MONTH.fullmatch(range_name):  # A calendar month, e.g. 2026-03; not one that hasn't started yet.
        if not (1 <= int(range_name[5:]) <= 12 and '2000' <= range_name <= current.astimezone(zone).strftime('%Y-%m')):
            raise HTTPException(400, f'range {range_name} is not a valid past or current month')
    elif range_name not in RANGES:
        raise HTTPException(400, f'range must be one of {", ".join(RANGES)}, YYYY-MM or all')
    start, end, granularity, buckets = time_range(range_name, current, zone)
    return utc_iso(start), utc_iso(end), granularity, buckets, zone
