"""Coverage-gap tests for the accounting/reconciliation requirements.

Each test asserts a business rule (not the implementation's arithmetic). Several were first
written as expected failures that exposed production bugs; those bugs are fixed and the tests
now guard against regressions.
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Config, MatchingConfig
from backend.db.connection import connect
from backend.services.financials.aggregation import series, summarize, time_range, utc_iso
from backend.services.financials.calculator import compute
from backend.services.financials.export import EXPORT_FIELDS, export_csv
from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual
from backend.services.reconciliation.matcher import run_matching
from backend.services.sync.ebay_sync import store_orders, store_returns, store_transactions
from backend.services.sync.orchestrator import GMAIL_MESSAGES_PER_CYCLE, Skipped, SyncContext, step_gmail, store_email
from factories import amazon_email, charge, ebay_order, sale_transaction

CFG = MatchingConfig()
UTC = timezone.utc
NY = ZoneInfo('America/New_York')
T0 = datetime(2026, 9, 20, 15, tzinfo=UTC)  # Default factory order time.
REPO = Path(__file__).resolve().parent.parent


def z(moment, millis=False):
    """UTC ISO stamp; eBay creationDate carries milliseconds, stored emails do not."""
    text = moment.astimezone(UTC).isoformat(timespec='milliseconds' if millis else 'seconds')
    return text.replace('+00:00', 'Z')


def at(**delta):
    return z(T0 + timedelta(**delta))


def order_at(order_id, moment, **kwargs):
    return ebay_order(order_id, created=z(moment, millis=True), **kwargs)


def seed(db, *orders, now=T0 + timedelta(hours=2)):
    with db:
        store_orders(db, list(orders), now)


def by_id(db):
    return {o['order_id']: o for o in load_orders(db)}


def match_rows(db):
    return {r['order_id']: dict(r) for r in db.execute('SELECT * FROM order_matches')}


def effective(order):
    return order['effective_revenue_cents'], order['effective_cost_cents'], order['effective_profit_cents']


RETURN_OPEN = {'orderId': 'A', 'returnId': 'r1', 'state': 'ITEM_SHIPPED', 'status': 'RETURN_REQUESTED'}
RETURN_DONE = {'orderId': 'A', 'returnId': 'r1', 'state': 'CLOSED', 'status': 'CLOSED',
               'sellerTotalRefund': {'actualRefundAmount': {'value': '50.00'}}}
RETURN_WITHDRAWN = {'orderId': 'A', 'returnId': 'r1', 'state': 'RETURN_REQUEST_CANCELLED', 'status': 'CANCELLED'}


# --------------------------------------------------------------------------- duplicate eBay syncs

def test_replayed_and_overlapping_finance_pages_count_each_charge_once(db):
    seed(db, ebay_order('A', payment='PARTIALLY_REFUNDED'))
    sale, ad, refund = sale_transaction('A', 40.0), charge('A', 2.5), charge('A', 5.0, kind='REFUND', fee_type=None)
    with db:
        store_transactions(db, [sale, ad], T0)
        store_transactions(db, [sale, ad], T0)  # Same page replayed.
        store_transactions(db, [ad, refund], T0)  # Overlapping window adds one new transaction.
        store_orders(db, [ebay_order('A', payment='PARTIALLY_REFUNDED')], T0)  # Order re-sync afterwards.
    order = by_id(db)['A']
    # Paid out: $40.00 sale, less the $2.50 ad fee and the $5.00 partial refund, each exactly once.
    assert order['net_revenue_cents'] == 3250 and order['revenue_source'] == 'finances'
    assert db.execute('SELECT count(*) FROM ebay_transactions').fetchone()[0] == 3
    assert not order['excluded']  # A partial refund is not a fully refunded order.


def test_resync_never_resets_matches_manual_cost_or_return_override(db):
    orders = [order_at('A', T0), order_at('B', T0, name='Bob Smith', city='Peoria')]
    seed(db, *orders)
    with db:
        amazon_email(db, 'm1', at(minutes=5), total_cents=2750)
        run_matching(db, CFG, T0 + timedelta(hours=1))
        manual.set_manual_cost(db, 'B', 1234, T0)
        store_returns(db, [RETURN_DONE], T0)
        manual.set_return_status(db, 'A', 'RETURN_CANCELLED', T0)  # User knows the return fell through.
    before = match_rows(db)
    with db:
        for _ in range(2):  # Duplicate and overlapping syncs of orders and returns.
            store_orders(db, orders, T0 + timedelta(hours=3))
            store_returns(db, [RETURN_DONE], T0 + timedelta(hours=3))
    assert match_rows(db) == before
    assert db.execute('SELECT count(*) FROM returns').fetchone()[0] == 1
    orders_now = by_id(db)
    assert (orders_now['A']['match_status'], orders_now['A']['amazon_cost_cents']) == ('MATCHED', 2750)
    assert orders_now['A']['display_status'] == 'Completed' and orders_now['A']['effective_profit_cents'] == 3900 - 2750
    assert orders_now['B']['amazon_cost_cents'] == 1234


# --------------------------------------------------------------------------- Gmail deduplication

def gmail_message(message_id, minute=5, body=None):
    stamp = datetime(2026, 9, 24, 14, minute % 60, tzinfo=UTC).timestamp()
    return {'id': message_id, 'internal_date_ms': int(stamp * 1000), 'subject': 'Ordered: "Lug Nuts 20pc"',
            'body': body or '<p>Ship to: JANE DOE - SPRINGFIELD, IL 62704</p><p>Quantity: 1</p>'
                            '<p>Grand Total: $27.50</p>', 'is_html': True}


def test_storing_the_same_gmail_message_twice_keeps_one_original_row(db):
    now = datetime(2026, 9, 24, 15, tzinfo=UTC)
    store_email(db, gmail_message('m1'), now)
    store_email(db, gmail_message('m1', body='<p>Grand Total: $99.99</p>'), now + timedelta(minutes=1))
    rows = db.execute('SELECT grand_total_cents, parse_status FROM amazon_email_purchases').fetchall()
    assert [tuple(r) for r in rows] == [(2750, 'parsed')]


class FakeGmail:
    def __init__(self, ids):
        self.ids = ids
        self.fetched = []

    def search(self, query):
        return list(self.ids)

    def get(self, message_id):
        self.fetched.append(message_id)
        return gmail_message(message_id, minute=len(self.fetched))


def test_gmail_backlog_larger_than_one_cycle_is_fetched_once_and_nothing_is_lost(db):
    ids = [f'm{i:03d}' for i in range(GMAIL_MESSAGES_PER_CYCLE + 5)]
    gmail = FakeGmail(ids)
    ctx = SyncContext(Config(), gmail=gmail)
    now = datetime(2026, 9, 24, 15, tzinfo=UTC)
    with pytest.raises(Skipped):  # More than one cycle's worth: continue next cycle.
        step_gmail(db, ctx, now)
    step_gmail(db, ctx, now + timedelta(minutes=1))
    step_gmail(db, ctx, now + timedelta(minutes=2))  # Nothing new: nothing refetched.
    assert sorted(gmail.fetched) == ids  # Every message fetched exactly once.
    assert db.execute('SELECT count(*) FROM amazon_email_purchases').fetchone()[0] == len(ids)


def test_duplicate_ids_in_one_gmail_search_are_stored_once(db):
    ctx = SyncContext(Config(), gmail=FakeGmail(['m1', 'm2', 'm1']))
    step_gmail(db, ctx, datetime(2026, 9, 24, 15, tzinfo=UTC))
    assert db.execute('SELECT count(*) FROM amazon_email_purchases').fetchone()[0] == 2


# --------------------------------------------------------------------------- matching logic

def test_multi_line_order_matches_on_its_total_units(db):
    two_lines = [('111', 'Lug Nuts', 1, ''), ('222', 'Spark Plugs', 2, '')]
    seed(db, order_at('A', T0, items=two_lines),
         order_at('B', T0, items=two_lines, name='Bob Smith', city='Peoria'))
    with db:
        amazon_email(db, 'mA', at(minutes=5), quantity=3)  # One Amazon order for all 3 units.
        amazon_email(db, 'mB', at(minutes=5), first_name='Bob', city='Peoria', quantity=1)  # Only part of B.
        run_matching(db, CFG, T0 + timedelta(hours=1))
    rows = match_rows(db)
    assert (rows['A']['status'], rows['A']['gmail_message_id']) == ('MATCHED', 'mA')
    candidate = json.loads(rows['A']['candidates_json'])[0]
    assert candidate['signals']['quantity']['points'] > 0
    assert rows['B']['status'] != 'MATCHED' and rows['B']['gmail_message_id'] is None  # 1 of 3 units: no auto cost.


def test_partial_emails_are_never_auto_matched_but_can_be_linked_by_hand(db):
    seed(db, order_at('A', T0))
    with db:
        amazon_email(db, 'm1', at(minutes=5), first_name=None, city=None, state=None, status='partial',
                     total_cents=1800)
        run_matching(db, CFG, T0 + timedelta(hours=1))
    assert match_rows(db)['A']['status'] == 'PENDING' and by_id(db)['A']['amazon_cost_cents'] is None
    with db:
        manual.link_email(db, 'A', 'm1', T0 + timedelta(hours=1))
    order = by_id(db)['A']
    assert (order['match_status'], order['amazon_cost_cents']) == ('MANUAL', 1800)


# --------------------------------------------------------------------------- 24-hour boundary

@pytest.mark.parametrize('email_delta,matched', [
    (timedelta(hours=24), True),
    (timedelta(hours=24, seconds=1), False),
    (timedelta(minutes=-15), True),
    (timedelta(minutes=-15, seconds=-1), False)])
def test_24_hour_window_boundary_through_the_matcher(db, email_delta, matched):
    seed(db, order_at('A', T0))  # eBay-style timestamp with milliseconds.
    with db:
        amazon_email(db, 'm1', z(T0 + email_delta))
        run_matching(db, CFG, T0 + timedelta(hours=25))
    row = match_rows(db)['A']
    if matched:
        assert (row['status'], row['gmail_message_id']) == ('MATCHED', 'm1')
    else:
        assert (row['status'], row['gmail_message_id'], json.loads(row['candidates_json'])) == ('PENDING', None, [])


# --------------------------------------------------------------------------- ambiguous matches

def test_contested_email_goes_to_review_when_the_stronger_order_does_not_lead_by_the_margin(db):
    # Both orders fit the email; A is closer in time (stronger) but only by a few points.
    seed(db, order_at('A', T0), order_at('B', T0 - timedelta(hours=10)))
    with db:
        amazon_email(db, 'm1', at(minutes=5))
        run_matching(db, CFG, T0 + timedelta(hours=1))
    rows = match_rows(db)
    scores = {o: rows[o]['confidence'] for o in 'AB'}
    assert scores['A'] > scores['B'] >= CFG.auto_match_score
    assert scores['A'] - scores['B'] < CFG.auto_match_margin
    assert {(rows[o]['status'], rows[o]['gmail_message_id']) for o in 'AB'} == {('NEEDS_REVIEW', None)}
    assert by_id(db)['A']['amazon_cost_cents'] is None


def test_clearly_stronger_order_takes_contested_email_and_rival_is_never_given_it(db):
    seed(db, order_at('A', T0), order_at('B', T0, name='Joan Doe'))  # B: first name disagrees.
    with db:
        amazon_email(db, 'm1', at(minutes=5), total_cents=2750)
        run_matching(db, CFG, T0 + timedelta(hours=1))
        run_matching(db, CFG, T0 + timedelta(hours=2))  # Next cycle.
    rows = match_rows(db)
    assert (rows['A']['status'], rows['A']['gmail_message_id']) == ('MATCHED', 'm1')
    assert rows['B']['gmail_message_id'] is None and rows['B']['status'] == 'PENDING'
    orders = by_id(db)
    assert (orders['A']['amazon_cost_cents'], orders['B']['amazon_cost_cents']) == (2750, None)


# --------------------------------------------------------------------------- unmatched orders

def test_weak_candidate_leaves_order_pending_with_the_evidence_kept(db):
    seed(db, order_at('A', T0))
    with db:  # Right state and name, but wrong quantity and city.
        amazon_email(db, 'm1', at(minutes=5), quantity=2, city='Chicago')
        run_matching(db, CFG, T0 + timedelta(hours=1))
    row = match_rows(db)['A']
    assert (row['status'], row['gmail_message_id']) == ('PENDING', None)
    assert [c['gmail_message_id'] for c in json.loads(row['candidates_json'])] == ['m1']
    order = by_id(db)['A']
    assert order['amazon_cost_cents'] is None and order['effective_profit_cents'] is None
    cards = summarize(load_orders(db))
    assert (cards['total_orders'], cards['awaiting_cost'], cards['profit_cents'], cards['net_revenue_cents']) == (1, 1, 0, 3900)


# --------------------------------------------------------------------------- manual override

def test_manual_cost_beats_the_matched_email_and_clearing_it_falls_back_to_the_email(db):
    seed(db, order_at('A', T0))
    with db:
        amazon_email(db, 'm1', at(minutes=5), total_cents=2750)
        run_matching(db, CFG, T0 + timedelta(hours=1))
        manual.set_manual_cost(db, 'A', 1999, T0 + timedelta(hours=1))
        run_matching(db, CFG, T0 + timedelta(hours=2))
    order = by_id(db)['A']
    assert (order['match_status'], order['gmail_message_id'], order['amazon_cost_cents']) == ('MANUAL', 'm1', 1999)
    with db:
        manual.set_manual_cost(db, 'A', None, T0 + timedelta(hours=2))
        run_matching(db, CFG, T0 + timedelta(hours=3))
    order = by_id(db)['A']
    assert (order['match_status'], order['gmail_message_id'], order['amazon_cost_cents']) == ('MANUAL', 'm1', 2750)


def test_manual_link_ignores_the_window_and_unlink_keeps_a_manual_cost(db):
    seed(db, order_at('A', T0))
    with db:
        amazon_email(db, 'late', at(days=3), total_cents=4100)  # Outside the automatic window.
        run_matching(db, CFG, T0 + timedelta(hours=1))
        assert match_rows(db)['A']['status'] == 'PENDING'
        manual.link_email(db, 'A', 'late', T0 + timedelta(days=3))
    assert by_id(db)['A']['amazon_cost_cents'] == 4100
    with db:
        manual.set_manual_cost(db, 'A', 3000, T0 + timedelta(days=3))
        manual.unlink(db, 'A', T0 + timedelta(days=3))
        run_matching(db, CFG, T0 + timedelta(days=3), order_ids=['A'])
    order = by_id(db)['A']
    assert (order['match_status'], order['gmail_message_id'], order['amazon_cost_cents']) == ('MANUAL', None, 3000)


def test_manual_return_status_wins_over_ebay_until_cleared(db):
    seed(db, order_at('A', T0))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        store_returns(db, [RETURN_DONE], T0)
    assert by_id(db)['A']['display_status'] == 'Returned'
    with db:
        manual.set_return_status(db, 'A', 'RETURN_CANCELLED', T0)
        store_returns(db, [RETURN_DONE], T0 + timedelta(hours=1))  # eBay keeps reporting the old state.
    order = by_id(db)['A']
    assert (order['return_status'], order['effective_profit_cents'], order['excluded']) == ('RETURN_CANCELLED', 1400, False)
    with db:
        manual.set_return_status(db, 'A', None, T0 + timedelta(hours=2))
    assert by_id(db)['A']['return_status'] == 'RETURN_COMPLETED'


# --------------------------------------------------------------------------- financial calculations

def test_profit_uses_what_ebay_actually_paid_after_fees_ads_and_partial_refunds(db):
    seed(db, order_at('A', T0, payment='PARTIALLY_REFUNDED'))
    with db:
        store_transactions(db, [sale_transaction('A', 44.0, gross=55.0, fvf=6.0, ad=1.0),
                                charge('A', 3.0, fee_type='AD_FEE'),
                                charge('A', 10.0, kind='REFUND', fee_type=None)], T0)
        manual.set_manual_cost(db, 'A', 2000, T0)
    order = by_id(db)['A']
    # eBay paid $44.00 for the sale, then took $3.00 in ads and $10.00 back as a partial refund.
    assert (order['net_revenue_cents'], order['effective_profit_cents']) == (3100, 1100)
    assert order['margin'] == pytest.approx(11 / 31, abs=1e-4) and order['roi'] == pytest.approx(11 / 20, abs=1e-4)
    assert order['display_status'] == 'Completed' and not order['excluded']


def test_losses_are_negative_and_zero_cost_has_no_roi():
    loss = compute(is_cancelled=False, return_status='NONE', net_revenue_cents=2000, amazon_cost_cents=2500)
    assert (loss.effective_profit_cents, loss.margin, loss.roi) == (-500, -0.25, -0.2)
    free = compute(is_cancelled=False, return_status='NONE', net_revenue_cents=2000, amazon_cost_cents=0)
    assert (free.effective_profit_cents, free.roi, free.cost_known) == (2000, None, True)


def test_cards_average_and_margin_count_only_costed_orders(db):
    # Net revenue estimates: $40 (costed $25), $20 (costed $25, a loss), $30 (cost unknown).
    seed(db, order_at('A', T0, due=45, fee=5), order_at('B', T0, due=25, fee=5), order_at('C', T0, due=35, fee=5))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        manual.set_manual_cost(db, 'B', 2500, T0)
    cards = summarize(load_orders(db))
    assert (cards['total_orders'], cards['net_revenue_cents'], cards['amazon_cost_cents']) == (3, 9000, 5000)
    assert (cards['profit_cents'], cards['avg_profit_per_order_cents'], cards['awaiting_cost']) == (1000, 500, 1)
    assert cards['profit_margin'] == pytest.approx(1000 / 6000, abs=1e-4)  # Unknown-cost revenue excluded.


# --------------------------------------------------------------------------- cancelled / returns / refunds

def test_order_cancelled_after_matching_is_zeroed_everywhere_but_keeps_its_history(db):
    order = order_at('A', datetime(2026, 9, 23, 16, tzinfo=UTC), items=[('111', 'Lug Nuts', 2, '')])
    seed(db, order)
    with db:
        store_transactions(db, [sale_transaction('A', 40.0)], T0)
        amazon_email(db, 'm1', '2026-09-23T16:05:00Z', total_cents=2500, quantity=2)
        run_matching(db, CFG, datetime(2026, 9, 23, 17, tzinfo=UTC))
        store_orders(db, [order_at('A', datetime(2026, 9, 23, 16, tzinfo=UTC), items=[('111', 'Lug Nuts', 2, '')],
                                   cancelled=True)], T0)
    row = by_id(db)['A']
    assert effective(row) == (0, 0, 0) and row['excluded'] and row['display_status'] == 'Cancelled'
    assert (row['net_revenue_cents'], row['amazon_cost_cents'], row['original_profit_cents']) == (4000, 2500, 1500)
    cards = summarize([row])
    assert (cards['total_orders'], cards['net_revenue_cents'], cards['profit_cents']) == (0, 0, 0)
    assert cards['excluded']['cancelled'] == 1
    now = datetime(2026, 9, 24, 18, tzinfo=UTC)
    points = series([row], time_range('1w', now, NY)[3], 'day', NY)
    assert all(p['orders'] == p['revenue_cents'] == p['cost_cents'] == p['profit_cents'] == 0 for p in points)
    assert db.execute('SELECT quantity FROM sales').fetchone()[0] == 0  # Sourcing units too.


def test_return_started_on_ebay_keeps_profit_and_is_flagged_on_the_cards(db):
    seed(db, order_at('A', T0))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        store_returns(db, [RETURN_OPEN], T0)
    row = by_id(db)['A']
    assert (row['display_status'], row['effective_profit_cents'], row['excluded']) == ('Return pending', 1400, False)
    cards = summarize([row])
    assert (cards['return_pending'], cards['profit_cents'], cards['total_orders']) == (1, 1400, 1)


@pytest.mark.parametrize('sequence', ['return_then_refund', 'refund_then_return'])
def test_completed_return_zeroes_profit_whichever_ebay_signal_arrives_first(db, sequence):
    seed(db, order_at('A', T0))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        refunded = order_at('A', T0, payment='FULLY_REFUNDED')
        if sequence == 'return_then_refund':
            store_returns(db, [RETURN_DONE], T0)
            store_orders(db, [refunded], T0 + timedelta(hours=1))
        else:
            store_orders(db, [refunded], T0)
            store_returns(db, [RETURN_DONE], T0 + timedelta(hours=1))
    row = by_id(db)['A']
    assert effective(row) == (0, 0, 0) and row['excluded'] and row['display_status'] == 'Returned'
    assert row['original_profit_cents'] == 1400
    assert summarize([row])['excluded'] == {'cancelled': 0, 'returned': 1, 'refunded': 0}


def test_withdrawn_return_on_ebay_restores_normal_profit(db):
    seed(db, order_at('A', T0))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        store_returns(db, [RETURN_OPEN], T0)
        store_returns(db, [RETURN_WITHDRAWN], T0 + timedelta(days=2))
    row = by_id(db)['A']
    assert (row['return_status'], row['display_status']) == ('RETURN_CANCELLED', 'Completed')
    assert (row['effective_profit_cents'], row['excluded'], 'RETURN_PENDING' in row['flags']) == (1400, False, False)
    assert summarize([row])['profit_cents'] == 1400


def test_fully_refunded_payment_status_zeroes_the_order_in_ledger_cards_and_csv(db):
    seed(db, order_at('A', T0, payment='FULLY_REFUNDED'))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
    row = by_id(db)['A']
    assert effective(row) == (0, 0, 0) and row['display_status'] == 'Refunded' and row['original_profit_cents'] == 1400
    cards = summarize([row])
    assert (cards['total_orders'], cards['profit_cents'], cards['excluded']['refunded']) == (0, 0, 1)
    exported = next(csv.DictReader(io.StringIO(export_csv([row]))))
    assert (exported['display_status'], exported['return_status'], exported['effective_profit']) == ('Refunded', 'REFUNDED', '0.00')
    assert exported['original_profit'] == '14.00'


def test_open_return_does_not_undo_a_full_refund(db):
    """Regression: an open return case once overwrote REFUNDED and put the profit back on the dashboard."""
    seed(db, order_at('A', T0, payment='FULLY_REFUNDED'))
    with db:
        manual.set_manual_cost(db, 'A', 2500, T0)
        store_returns(db, [RETURN_OPEN], T0 + timedelta(minutes=10))  # Returns step runs every 10 minutes.
    row = by_id(db)['A']
    assert row['excluded'] and effective(row) == (0, 0, 0)


# --------------------------------------------------------------------------- CSV output

def test_export_of_no_orders_is_just_the_header():
    assert list(csv.reader(io.StringIO(export_csv([])))) == [EXPORT_FIELDS]


def test_negative_money_in_the_export_stays_numeric(db):
    seed(db, order_at('A', T0))  # Net revenue $39.00 (estimate).
    with db:
        manual.set_manual_cost(db, 'A', 5000, T0)  # Sold at a loss.
    row = next(csv.DictReader(io.StringIO(export_csv(load_orders(db)))))
    assert float(row['original_profit']) == -11.0 and float(row['effective_profit']) == -11.0
    assert float(row['profit_margin']) < 0 and float(row['roi']) < 0


# --------------------------------------------------------------------------- dashboard ranges

def test_1w_daily_buckets_follow_local_days_across_the_dst_change(db):
    now = datetime(2026, 11, 4, 17, tzinfo=UTC)  # DST ended Sun Nov 1 at 02:00 local.
    moments = {'before_range': datetime(2026, 10, 29, 3, 59, tzinfo=UTC),  # Oct 28 23:59 EDT
               'first_day': datetime(2026, 10, 29, 4, 1, tzinfo=UTC),  # Oct 29 00:01 EDT
               'dst_edt': datetime(2026, 11, 1, 5, 30, tzinfo=UTC),  # Nov 1 01:30 EDT
               'dst_est': datetime(2026, 11, 1, 6, 30, tzinfo=UTC),  # Nov 1 01:30 EST (repeated hour)
               'late_nov1': datetime(2026, 11, 2, 4, 30, tzinfo=UTC),  # Nov 1 23:30 EST (UTC says Nov 2)
               'early_nov2': datetime(2026, 11, 2, 5, 30, tzinfo=UTC)}  # Nov 2 00:30 EST
    seed(db, *[order_at(name, moment) for name, moment in moments.items()], now=now)
    start, end, granularity, buckets = time_range('1w', now, NY)
    orders = load_orders(db, utc_iso(start), utc_iso(end))
    assert 'before_range' not in {o['order_id'] for o in orders}
    counts = {p['label']: p['orders'] for p in series(orders, buckets, granularity, NY)}
    assert list(counts) == ['Oct 29', 'Oct 30', 'Oct 31', 'Nov 01', 'Nov 02', 'Nov 03', 'Nov 04']
    assert counts == {'Oct 29': 1, 'Oct 30': 0, 'Oct 31': 0, 'Nov 01': 3, 'Nov 02': 1, 'Nov 03': 0, 'Nov 04': 0}


@pytest.mark.parametrize('now, hours', [
    (datetime(2026, 11, 1, 17, tzinfo=UTC), 25),  # Noon EST on the fall-back day.
    (datetime(2026, 3, 8, 16, tzinfo=UTC), 23),  # Noon EDT on the spring-forward day.
])
def test_1d_is_the_local_calendar_day_with_real_hours_on_dst_days(now, hours):
    start, end, _, buckets = time_range('1d', now, NY)
    day = now.astimezone(NY).date()
    assert (start, end) == (datetime(day.year, day.month, day.day, tzinfo=NY), datetime(day.year, day.month, day.day + 1, tzinfo=NY))
    instants = [b.astimezone(UTC) for b in buckets]
    assert len(buckets) == hours and end.astimezone(UTC) - start.astimezone(UTC) == timedelta(hours=hours)
    assert all(b - a == timedelta(hours=1) for a, b in zip(instants, instants[1:]))


def test_1d_starts_at_local_midnight_even_just_after_it():
    now = datetime(2026, 9, 24, 4, 15, tzinfo=UTC)  # 00:15 EDT; UTC says 04:15.
    start, end, _, buckets = time_range('1d', now, NY)
    assert start == datetime(2026, 9, 24, tzinfo=NY) and (start.hour, start.minute) == (0, 0)
    assert end == datetime(2026, 9, 25, tzinfo=NY) and len(buckets) == 24


def test_1d_excludes_late_yesterday_and_includes_early_today(db):
    now = datetime(2026, 9, 24, 18, tzinfo=UTC)  # 14:00 EDT.
    seed(db, order_at('yesterday', datetime(2026, 9, 23, 23, 30, tzinfo=NY)),
         order_at('today', datetime(2026, 9, 24, 0, 30, tzinfo=NY)), now=now)
    start, end, granularity, buckets = time_range('1d', now, NY)
    orders = load_orders(db, utc_iso(start), utc_iso(end))
    assert [o['order_id'] for o in orders] == ['today']
    points = {p['label']: p['orders'] for p in series(orders, buckets, granularity, NY)}
    assert points['Sep 24 00:00'] == 1 and sum(points.values()) == 1


def test_1d_labels_run_from_midnight_through_11_pm():
    now = datetime(2026, 9, 24, 18, tzinfo=UTC)
    _, _, granularity, buckets = time_range('1d', now, NY)
    labels = [p['label'] for p in series([], buckets, granularity, NY)]
    assert labels == [f'Sep 24 {h:02d}:00' for h in range(24)]


def test_month_buckets_across_the_year_boundary(db):
    now = datetime(2027, 1, 15, 17, tzinfo=UTC)
    moments = {'jan_2026': datetime(2026, 1, 20, 17, tzinfo=UTC),  # Older than 12 months.
               'feb_2026': datetime(2026, 2, 1, 5, 30, tzinfo=UTC),  # Feb 1 00:30 EST, first 12m bucket.
               'nye': datetime(2027, 1, 1, 4, 30, tzinfo=UTC),  # Dec 31 23:30 EST (UTC says Jan 1).
               'new_year': datetime(2027, 1, 1, 5, 30, tzinfo=UTC)}  # Jan 1 00:30 EST.
    seed(db, *[order_at(name, moment) for name, moment in moments.items()], now=now)

    def points(range_name):
        start, end, granularity, buckets = time_range(range_name, now, NY)
        orders = load_orders(db, utc_iso(start), utc_iso(end))
        return {p['label']: p['orders'] for p in series(orders, buckets, granularity, NY)}, {o['order_id'] for o in orders}

    twelve, twelve_ids = points('12m')
    assert list(twelve)[0] == 'Feb 2026' and list(twelve)[-1] == 'Jan 2027' and len(twelve) == 12
    assert (twelve['Feb 2026'], twelve['Dec 2026'], twelve['Jan 2027']) == (1, 1, 1)
    assert twelve_ids == {'feb_2026', 'nye', 'new_year'}
    ytd, ytd_ids = points('ytd')
    assert ytd == {'Jan 2027': 1} and ytd_ids == {'new_year'}  # New Year's Eve (local) is last year.


def test_order_in_the_first_second_of_a_range_belongs_to_that_range(db):
    now = datetime(2027, 1, 15, 17, tzinfo=UTC)
    midnight = datetime(2027, 1, 1, tzinfo=NY)  # Local midnight, New Year's Day.
    seed(db, order_at('A', midnight), now=now)
    start, end, *_ = time_range('ytd', now, NY)
    assert [o['order_id'] for o in load_orders(db, utc_iso(start), utc_iso(end))] == ['A']
    assert load_orders(db, None, utc_iso(start)) == []


# --------------------------------------------------------------------------- through the API

FIXED_NOW = datetime(2026, 9, 24, 18, tzinfo=UTC)


@pytest.fixture
def api(tmp_path, monkeypatch):
    """App on a temp DB with the sync loop off, New York buckets and a frozen dashboard clock."""
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads((REPO / 'config.json').read_text())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    config['accounting'] = dict(config['accounting'], timezone='America/New_York',
                                ordering_fee_cents=0)  # Fee rules: test_ordering_fee.py.
    config['sync'] = dict(config['sync'], enabled=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    clock = {'now': FIXED_NOW}
    monkeypatch.setattr('backend.api.deps.now', lambda: clock['now'])
    db = connect(config['database'])
    with TestClient(app) as client:
        client.db, client.clock = db, clock
        yield client
    db.close()


def test_api_return_override_restore_path(api):
    seed(api.db, order_at('A', FIXED_NOW - timedelta(days=2)))
    with api.db:
        store_returns(api.db, [RETURN_OPEN], FIXED_NOW)
    assert api.post('/api/orders/A/amazon-cost', json={'amount': '25.00'}).json()['effective_profit_cents'] == 1400
    completed = api.put('/api/orders/A/return-status', json={'status': 'RETURN_COMPLETED'}).json()
    assert (completed['display_status'], completed['effective_profit_cents'], completed['original_profit_cents']) == ('Returned', 0, 1400)
    assert api.get('/api/overview?range=1w').json()['cards']['profit_cents'] == 0
    restored = api.put('/api/orders/A/return-status', json={'status': 'RETURN_CANCELLED'}).json()
    assert (restored['display_status'], restored['effective_profit_cents'], restored['excluded']) == ('Completed', 1400, False)
    cards = api.get('/api/overview?range=1w').json()['cards']
    assert (cards['profit_cents'], cards['excluded']['returned']) == (1400, 0)
    ebay_again = api.put('/api/orders/A/return-status', json={'status': None}).json()
    assert (ebay_again['return_status'], ebay_again['display_status']) == ('RETURN_STARTED', 'Return pending')


def test_api_manual_cost_parsing_and_validation(api):
    seed(api.db, order_at('A', FIXED_NOW - timedelta(days=1)))
    assert api.post('/api/orders/A/amazon-cost', json={'amount': '$1,234.56'}).json()['amazon_cost_cents'] == 123456
    assert api.post('/api/orders/A/amazon-cost', json={'amount': '-5'}).status_code == 400
    assert api.post('/api/orders/missing/amazon-cost', json={'amount': '5'}).status_code == 400
    cleared = api.post('/api/orders/A/amazon-cost', json={'amount': None}).json()
    assert (cleared['amazon_cost_cents'], cleared['match_status'], cleared['cost_known']) == (None, 'PENDING', False)


def test_api_recheck_keeps_manual_link_and_replace_moves_the_email(api):
    created = FIXED_NOW - timedelta(hours=3)
    seed(api.db, order_at('A', created), order_at('B', created))
    with api.db:
        amazon_email(api.db, 'm1', z(created + timedelta(minutes=5)), total_cents=2100)
    assert api.post('/api/orders/A/match', json={'gmail_message_id': 'm1'}).json()['match_status'] == 'MANUAL'
    assert api.post('/api/orders/A/recheck').json()['gmail_message_id'] == 'm1'
    moved = api.post('/api/orders/B/match', json={'gmail_message_id': 'm1', 'replace': True}).json()
    assert (moved['match_status'], moved['amazon_cost_cents']) == ('MANUAL', 2100)
    left = api.get('/api/orders/A').json()
    assert (left['gmail_message_id'], left['amazon_cost_cents'], left['match_status']) == (None, None, 'PENDING')
    assert api.post('/api/orders/A/recheck').json()['gmail_message_id'] is None  # Never re-offered.


def test_api_filters_and_cards_for_unmatched_and_review_orders(api):
    created = FIXED_NOW - timedelta(hours=3)
    seed(api.db, order_at('A', created), order_at('B', created), order_at('C', created, name='Carl Smith', city='Peoria'))
    with api.db:
        amazon_email(api.db, 'm1', z(created + timedelta(minutes=5)))  # A and B tie: review.
        run_matching(api.db, CFG, FIXED_NOW)
    cards = api.get('/api/overview?range=1d').json()['cards']
    assert (cards['total_orders'], cards['needs_review'], cards['awaiting_cost']) == (3, 2, 3)
    review = api.get('/api/orders?range=1d&match_status=NEEDS_REVIEW').json()
    assert sorted(o['order_id'] for o in review['orders']) == ['A', 'B']
    unknown = api.get('/api/orders?range=1d&match_status=COST_UNKNOWN').json()
    assert unknown['total'] == 3
    pending = api.get('/api/orders?range=1d&match_status=PENDING').json()
    assert [o['order_id'] for o in pending['orders']] == ['C']


def test_api_export_all_includes_cancelled_and_old_orders_filtered_keeps_cancelled(api):
    seed(api.db, order_at('OLD', datetime(2025, 6, 1, 12, tzinfo=UTC)),
         order_at('A', FIXED_NOW - timedelta(days=3)),
         order_at('C', FIXED_NOW - timedelta(days=2), cancelled=True))
    everything = list(csv.DictReader(io.StringIO(api.get('/api/orders/export?scope=all').text)))
    assert [r['ebay_order_id'] for r in everything] == ['OLD', 'A', 'C']  # Oldest first.
    cancelled = everything[2]
    assert (cancelled['display_status'], cancelled['ebay_status'], cancelled['effective_profit']) == ('Cancelled', 'CANCELLED', '0.00')
    response = api.get('/api/orders/export?scope=filtered&range=30d')
    assert response.headers['content-disposition'] == 'attachment; filename="orders-30d.csv"'
    assert [r['ebay_order_id'] for r in csv.DictReader(io.StringIO(response.text))] == ['A', 'C']


def test_api_overview_year_boundary_and_dst_ranges(api):
    seed(api.db, order_at('nye', datetime(2027, 1, 1, 4, 30, tzinfo=UTC)),
         order_at('new_year', datetime(2027, 1, 1, 5, 30, tzinfo=UTC)),
         order_at('dst', datetime(2026, 11, 2, 4, 30, tzinfo=UTC)))  # Nov 1 23:30 EST
    api.clock['now'] = datetime(2027, 1, 15, 17, tzinfo=UTC)
    ytd = api.get('/api/overview?range=ytd').json()
    assert (ytd['granularity'], ytd['timezone'], ytd['cards']['total_orders']) == ('month', 'America/New_York', 1)
    assert [(p['label'], p['orders']) for p in ytd['series']] == [('Jan 2027', 1)]
    twelve = {p['label']: p['orders'] for p in api.get('/api/overview?range=12m').json()['series']}
    assert (len(twelve), twelve['Nov 2026'], twelve['Dec 2026'], twelve['Jan 2027']) == (12, 1, 1, 1)
    api.clock['now'] = datetime(2026, 11, 4, 17, tzinfo=UTC)
    week = {p['label']: p['orders'] for p in api.get('/api/overview?range=1w').json()['series']}
    assert week['Nov 01'] == 1 and week['Nov 02'] == 0
