import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from backend.services.financials.aggregation import series, summarize, time_range
from backend.services.financials.calculator import compute
from backend.services.financials.export import EXPORT_FIELDS, export_csv
from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual
from backend.services.sync.ebay_sync import store_orders, store_transactions
from factories import ebay_order, sale_transaction

NY = ZoneInfo('America/New_York')
NOW = datetime(2026, 9, 24, 18, 30, tzinfo=timezone.utc)


def calc(status='NONE', cancelled=False, revenue=4000, cost=2500):
    return compute(is_cancelled=cancelled, return_status=status, net_revenue_cents=revenue, amazon_cost_cents=cost)


def test_normal_profit_and_margin():
    f = calc()
    assert (f.original_profit_cents, f.effective_profit_cents, f.excluded) == (1500, 1500, False)
    assert (f.margin, f.roi) == (0.375, 0.6)


def test_unknown_cost_never_invents_profit():
    f = calc(cost=None)
    assert (f.original_profit_cents, f.effective_profit_cents, f.effective_revenue_cents) == (None, None, 4000)
    assert 'COST_UNKNOWN' in f.flags and not f.cost_known


def test_cancelled_order_is_zeroed_but_original_kept():
    f = calc(cancelled=True)
    assert (f.effective_revenue_cents, f.effective_cost_cents, f.effective_profit_cents) == (0, 0, 0)
    assert (f.original_profit_cents, f.net_revenue_cents, f.display_status, f.excluded) == (1500, 4000, 'Cancelled', True)


def test_return_started_keeps_profit_and_flags_it():
    f = calc('RETURN_STARTED')
    assert f.effective_profit_cents == 1500 and 'RETURN_PENDING' in f.flags and not f.excluded


@pytest.mark.parametrize('status,label', [('RETURN_COMPLETED', 'Returned'), ('REFUNDED', 'Refunded')])
def test_completed_return_and_refund_zero_effective_values(status, label):
    f = calc(status)
    assert (f.effective_revenue_cents, f.effective_cost_cents, f.effective_profit_cents) == (0, 0, 0)
    assert (f.original_profit_cents, f.display_status, f.excluded) == (1500, label, True)


def test_return_cancelled_restores_normal_profit():
    f = calc('RETURN_CANCELLED')
    assert (f.effective_profit_cents, f.excluded, f.display_status) == (1500, False, 'Completed')


def test_unknown_return_status_rejected():
    with pytest.raises(ValueError):
        calc('LOST')


def seeded(db):
    orders = [ebay_order('A', created='2026-09-24T14:00:00.000Z'),  # costed
              ebay_order('B', created='2026-09-24T03:00:00.000Z'),  # previous NY day, cost unknown
              ebay_order('C', created='2026-09-23T12:00:00.000Z', cancelled=True),
              ebay_order('D', created='2026-09-22T12:00:00.000Z', name='=cmd|evil')]  # refunded
    with db:
        store_orders(db, orders, NOW)
        store_transactions(db, [sale_transaction('A', 40.0)], NOW)
        manual.set_manual_cost(db, 'A', 2500, NOW)
        manual.set_manual_cost(db, 'C', 2500, NOW)
        manual.set_manual_cost(db, 'D', 1000, NOW)
        manual.set_return_status(db, 'D', 'REFUNDED', NOW)
    return load_orders(db)


def test_cards_exclude_cancelled_and_returned_and_flag_unknown_costs(db):
    cards = summarize(seeded(db))
    assert cards['total_orders'] == 2
    assert cards['net_revenue_cents'] == 4000 + 3900  # A (finances) + B (estimate); C, D excluded.
    assert (cards['amazon_cost_cents'], cards['profit_cents'], cards['awaiting_cost']) == (2500, 1500, 1)
    assert (cards['avg_profit_per_order_cents'], cards['profit_margin']) == (1500, 0.375)
    assert cards['excluded'] == {'cancelled': 1, 'returned': 0, 'refunded': 1}


def test_range_definitions_and_granularity():
    start, end, granularity, buckets = time_range('1d', NOW, NY)  # Today in NY (Sep 24), not a rolling 24 h.
    assert (granularity, len(buckets), end - start) == ('hour', 24, timedelta(hours=24))
    assert (start, end) == (datetime(2026, 9, 24, tzinfo=NY), datetime(2026, 9, 25, tzinfo=NY))
    assert buckets[-1] == datetime(2026, 9, 24, 23, tzinfo=NY)  # Future hours are still buckets.
    start, _, granularity, buckets = time_range('month', NOW, NY)
    assert (granularity, start.day, len(buckets)) == ('day', 1, 24)  # Calendar month to date.
    assert len(time_range('30d', NOW, NY)[3]) == 30 and len(time_range('1w', NOW, NY)[3]) == 7
    _, _, granularity, buckets = time_range('3m', NOW, NY)
    assert granularity == 'week' and len(buckets) == 13 and all(b.weekday() == 0 for b in buckets)
    _, _, granularity, buckets = time_range('12m', NOW, NY)
    assert granularity == 'month' and len(buckets) == 12 and buckets[0].month == 10 and buckets[0].year == 2025
    assert [b.month for b in time_range('ytd', NOW, NY)[3]] == list(range(1, 10))
    with pytest.raises(ValueError):
        time_range('5y', NOW, NY)


def test_series_buckets_in_local_time_with_empty_days(db):
    orders = seeded(db)
    _, _, granularity, buckets = time_range('1w', NOW, NY)
    points = {p['label']: p for p in series(orders, buckets, granularity, NY)}
    assert len(points) == 7
    assert points['Sep 24']['orders'] == 1 and points['Sep 24']['profit_cents'] == 1500  # A
    assert points['Sep 23']['orders'] == 1 and points['Sep 23']['cost_cents'] == 0  # B at 23:00 NY; C cancelled
    assert points['Sep 22']['orders'] == 0 and points['Sep 18']['revenue_cents'] == 0  # Refunded D; empty day


def test_export_has_all_columns_every_status_and_safe_cells(db):
    text = export_csv(sorted(seeded(db), key=lambda o: o['created_at']))
    rows = list(csv.DictReader(io.StringIO(text)))
    assert list(rows[0]) == EXPORT_FIELDS
    assert {r['display_status'] for r in rows} == {'Completed', 'Cancelled', 'Refunded'}
    by_id = {r['ebay_order_id']: r for r in rows}
    assert (by_id['A']['net_ebay_revenue'], by_id['A']['amazon_cost'], by_id['A']['effective_profit']) == ('40.00', '25.00', '15.00')
    assert (by_id['C']['original_profit'], by_id['C']['effective_profit']) == ('14.00', '0.00')
    assert by_id['B']['amazon_cost'] == '' and by_id['B']['amazon_match_status'] == 'PENDING'
    assert by_id['D']['buyer_name'].startswith("'=")  # Formula injection escaped.
    assert by_id['A']['asin'] == 'B0TESTASIN' and by_id['A']['ebay_final_value_fee'] == '6.00'


def test_dst_fall_back_hours_are_counted_once(db):
    now = datetime(2026, 11, 1, 17, tzinfo=timezone.utc)
    with db:
        store_orders(db, [ebay_order('EDT', created='2026-11-01T05:30:00.000Z'),   # 01:30 EDT
                          ebay_order('EST', created='2026-11-01T06:30:00.000Z')], now)  # 01:30 EST
    start, end, granularity, buckets = time_range('1d', now, NY)
    from backend.services.financials.aggregation import utc_iso
    points = series(load_orders(db, utc_iso(start), utc_iso(end)), buckets, granularity, NY)
    assert len(points) == 25 and sum(p['orders'] for p in points) == 2
    ones = [p for p in points if ' 01:00' in p['label']]
    assert [p['orders'] for p in ones] == [1, 1] and ones[0]['label'] != ones[1]['label']
