import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.config import MatchingConfig
from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual, matcher
from backend.services.reconciliation.matcher import now_iso, run_matching
from backend.services.reconciliation.scoring import norm_city, norm_state, score
from backend.services.sync.ebay_sync import store_orders
from factories import amazon_email, ebay_order

CFG = MatchingConfig()
ORDER_TIME = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
NOW = ORDER_TIME + timedelta(hours=2)


def at(minutes):
    return (ORDER_TIME + timedelta(minutes=minutes)).isoformat().replace('+00:00', 'Z')


ORDER = {'first_name': 'Jane', 'city': 'Springfield', 'state': 'IL', 'quantity': 1, 'created_at': at(0)}


def email(minutes=5, **fields):
    return {'first_name': 'Jane', 'city': 'Springfield', 'state': 'IL', 'quantity': 1, 'received_at': at(minutes), **fields}


def test_normalization_handles_formatting_differences():
    assert norm_city('St. Louis') == norm_city('saint louis ') == 'saint louis'
    assert norm_city('Ft Worth') == 'fort worth'
    assert norm_state('Missouri') == norm_state('mo') == 'MO'
    result = score({**ORDER, 'first_name': 'JANE Q', 'city': 'ST. LOUIS', 'state': 'Missouri'},
                   email(first_name=' jane', city='Saint Louis', state='MO'), CFG)
    assert result['signals']['first_name']['points'] == CFG.first_name_match
    assert result['signals']['city']['points'] == CFG.city_match
    assert result['signals']['state']['points'] == CFG.state_match


def test_signals_are_transparent_and_time_proximity_counts():
    close, late = score(ORDER, email(5), CFG), score(ORDER, email(20 * 60), CFG)
    assert set(close['signals']) == {'state', 'quantity', 'first_name', 'city', 'time'}
    assert close['score'] > late['score'] >= CFG.auto_match_score  # Both valid; closer is stronger.
    assert close['score'] == pytest.approx(sum(s['points'] for s in close['signals'].values()))
    assert score(ORDER, email(5, quantity=2), CFG)['signals']['quantity']['points'] == CFG.quantity_mismatch


@pytest.mark.parametrize('minutes,eligible', [(-15, True), (-16, False), (24 * 60, True), (24 * 60 + 1, False)])
def test_matching_window_boundaries(minutes, eligible):
    assert score(ORDER, email(minutes), CFG)['eligible'] is eligible


def test_state_mismatch_is_never_a_candidate():
    result = score(ORDER, email(5, state='Wisconsin'), CFG)
    assert not result['eligible'] and 'state' in result['reason']


def seed(db, *orders):
    with db:
        store_orders(db, list(orders), NOW)


def statuses(db):
    return {r['order_id']: (r['status'], r['gmail_message_id']) for r in db.execute('SELECT * FROM order_matches')}


def updated_at(db, order_id):
    return db.execute('SELECT updated_at FROM order_matches WHERE order_id=?', (order_id,)).fetchone()[0]


def fetched(db, moment, *message_ids):
    """When the sync cycle downloaded the emails (the factory stamps fetched_at = received_at)."""
    db.executemany('UPDATE amazon_email_purchases SET fetched_at=? WHERE gmail_message_id=?',
                   [(now_iso(moment), m) for m in message_ids])


def test_clear_candidate_auto_matches_and_sets_amazon_cost(db):
    seed(db, ebay_order('A', created=at(0)))
    with db:
        amazon_email(db, 'm1', at(4), total_cents=2750)
        run_matching(db, CFG, NOW)
    assert statuses(db) == {'A': ('MATCHED', 'm1')}
    order = load_orders(db)[0]
    assert (order['amazon_cost_cents'], order['original_profit_cents']) == (2750, 3900 - 2750)


def test_ambiguous_candidates_need_review(db):
    seed(db, ebay_order('A', created=at(0)))
    with db:
        amazon_email(db, 'm1', at(3))
        amazon_email(db, 'm2', at(6))
        run_matching(db, CFG, NOW)
    status, linked = statuses(db)['A']
    assert (status, linked) == ('NEEDS_REVIEW', None)
    candidates = json.loads(db.execute('SELECT candidates_json FROM order_matches').fetchone()[0])
    assert [c['gmail_message_id'] for c in candidates] == ['m1', 'm2']
    assert load_orders(db)[0]['amazon_cost_cents'] is None  # No invented cost.


def test_one_email_cannot_back_two_identical_orders(db):
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(1)))
    with db:
        amazon_email(db, 'm1', at(5))
        run_matching(db, CFG, NOW)
    assert {s for s, _ in statuses(db).values()} == {'NEEDS_REVIEW'}


def test_unmatched_orders_stay_pending_and_retry_on_later_cycles(db):
    seed(db, ebay_order('A', created=at(0)))
    with db:
        run_matching(db, CFG, NOW)
    assert statuses(db)['A'] == ('PENDING', None)
    with db:
        amazon_email(db, 'm1', at(10 * 60))  # Amazon order placed hours later.
        run_matching(db, CFG, ORDER_TIME + timedelta(hours=11))
    assert statuses(db)['A'] == ('MATCHED', 'm1')


def test_recheck_window_ends_but_manual_recheck_still_works(db):
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(1)))
    with db:
        amazon_email(db, 'm1', at(5))
        run_matching(db, CFG, NOW)  # A and B tie for m1: both wait for review.
        manual.set_manual_cost(db, 'B', 1234, NOW)  # B is settled by hand, so m1 is no longer contested.
    evaluated = updated_at(db, 'A')
    with db:  # Past recheck_hours and no email fetched since A's last evaluation: left alone.
        assert run_matching(db, CFG, ORDER_TIME + timedelta(hours=CFG.recheck_hours + 1)) == {}
    assert statuses(db)['A'] == ('NEEDS_REVIEW', None) and updated_at(db, 'A') == evaluated
    with db:  # The user's Recheck still evaluates it.
        assert run_matching(db, CFG, ORDER_TIME + timedelta(hours=40), order_ids=['A']) == {'MATCHED': 1}
    assert statuses(db)['A'] == ('MATCHED', 'm1')


def test_backfilled_order_is_matched_when_its_email_is_fetched_later(db):
    # History backfill (or Gmail disconnected): the order is first evaluated long after
    # recheck_hours, before its confirmation email has been downloaded.
    seed(db, ebay_order('A', created=at(0)))
    backfill = ORDER_TIME + timedelta(days=30)
    with db:
        assert run_matching(db, CFG, backfill) == {'PENDING': 1}
    later = backfill + timedelta(hours=CFG.recheck_hours)  # A later cycle downloads the email.
    with db:
        amazon_email(db, 'm1', at(4), total_cents=2750)
        fetched(db, later, 'm1')
        assert run_matching(db, CFG, later) == {'MATCHED': 1}
    assert statuses(db)['A'] == ('MATCHED', 'm1')
    assert load_orders(db)[0]['amazon_cost_cents'] == 2750


def test_old_order_is_rechecked_only_for_open_emails_in_its_window_fetched_since_its_evaluation(db):
    seed(db, ebay_order('A', created=at(0)))
    cycle = ORDER_TIME + timedelta(days=30)
    with db:
        run_matching(db, CFG, cycle)  # PENDING: no emails yet.
    cycle += timedelta(minutes=1)
    with db:  # New, but outside A's window, or not parsed: no reason to recheck A.
        amazon_email(db, 'later', at(2 * 24 * 60))
        amazon_email(db, 'partial', at(5), first_name=None, city=None, state=None, status='partial')
        fetched(db, cycle, 'later', 'partial')
        assert run_matching(db, CFG, cycle) == {}
    cycle += timedelta(minutes=1)
    with db:  # The Gmail step and reconcile share `now`: fetched and evaluated in the same cycle...
        amazon_email(db, 'm1', at(3))
        amazon_email(db, 'm2', at(6))
        fetched(db, cycle, 'm1', 'm2')
        assert run_matching(db, CFG, cycle) == {'NEEDS_REVIEW': 1}
    evaluated = updated_at(db, 'A')
    with db:  # ...so the next cycle has nothing new for A.
        assert run_matching(db, CFG, cycle + timedelta(minutes=1)) == {}
    assert updated_at(db, 'A') == evaluated


def test_contested_email_stays_in_review_when_only_one_contender_is_evaluated(db):
    # Same buyer, two orders 20 minutes apart, one Amazon email 5 minutes after the second.
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(20)))
    with db:
        amazon_email(db, 'm1', at(25))
        run_matching(db, CFG, NOW)
    both_in_review = {'A': ('NEEDS_REVIEW', None), 'B': ('NEEDS_REVIEW', None)}
    assert statuses(db) == both_in_review
    with db:  # Manual Recheck of A alone: B still wants m1.
        assert run_matching(db, CFG, NOW + timedelta(minutes=1), order_ids=['A']) == {'NEEDS_REVIEW': 1}
    assert statuses(db) == both_in_review
    later = ORDER_TIME + timedelta(hours=CFG.recheck_hours, minutes=10)
    with db:  # A has left the recheck window; B, still inside it, is evaluated alone.
        assert run_matching(db, CFG, later) == {'NEEDS_REVIEW': 1}
    assert statuses(db) == both_in_review
    with db:  # Once B lets go of m1 (linked by hand, then unlinked), it no longer competes.
        manual.link_email(db, 'B', 'm1', later)
        manual.unlink(db, 'B', later)
        run_matching(db, CFG, later, order_ids=['A'])
    assert statuses(db) == {'A': ('MATCHED', 'm1'), 'B': ('PENDING', None)}


def test_manual_links_made_while_the_matcher_runs_are_kept(db, monkeypatch):
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(0), name='Bob', city='Peoria'),
         ebay_order('C', created=at(0), name='Carl', city='Peoria'))
    with db:
        amazon_email(db, 'm1', at(3))
        amazon_email(db, 'm2', at(6))
        run_matching(db, CFG, NOW)
    before = {'A': ('NEEDS_REVIEW', None), 'B': ('PENDING', None), 'C': ('PENDING', None)}
    assert statuses(db) == before
    save, raced = matcher.save_evaluation, []

    def user_acts_between_reads_and_write(*args):
        if not raced:
            raced.append(True)
            manual.link_email(db, 'A', 'm2', NOW)  # The matcher is about to save A as NEEDS_REVIEW.
            manual.link_email(db, 'C', 'mb', NOW)  # The matcher is about to auto-claim mb for B.
        return save(*args)

    monkeypatch.setattr(matcher, 'save_evaluation', user_acts_between_reads_and_write)
    with db:
        amazon_email(db, 'mb', at(5), first_name='Bob', city='Peoria')  # Fits B clearly.
        assert run_matching(db, CFG, NOW + timedelta(minutes=1)) == {}  # No crash, nothing overwritten.
    assert raced and statuses(db) == {'A': ('MANUAL', 'm2'), 'B': ('PENDING', None), 'C': ('MANUAL', 'mb')}
    assert updated_at(db, 'B') == now_iso(NOW)  # B was skipped, left for the next cycle.
    monkeypatch.undo()
    with db:
        assert run_matching(db, CFG, NOW + timedelta(minutes=2)) == {'PENDING': 1}
    assert statuses(db)['B'] == ('PENDING', None) and statuses(db)['A'] == ('MANUAL', 'm2')


def test_manual_cost_link_and_unlink_are_never_overwritten(db):
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(0), name='Bob', city='Peoria'))
    with db:
        amazon_email(db, 'm1', at(5))
        amazon_email(db, 'm2', at(6), first_name='Bob', city='Peoria')
        run_matching(db, CFG, NOW)
        assert statuses(db)['A'] == ('MATCHED', 'm1')
        manual.unlink(db, 'A', NOW)  # Wrong match: detach.
        run_matching(db, CFG, NOW)
    assert statuses(db)['A'] == ('PENDING', None)  # m1 is never re-offered to A.
    with db:
        manual.link_email(db, 'A', 'm2', NOW, replace=True)  # Takes m2 away from B.
        run_matching(db, CFG, NOW)
    assert statuses(db)['A'] == ('MANUAL', 'm2')
    assert statuses(db)['B'][1] != 'm2'
    with db:
        manual.set_manual_cost(db, 'B', 1234, NOW)
        run_matching(db, CFG, NOW, order_ids=['A', 'B'])
    assert statuses(db)['B'][0] == 'MANUAL'
    by_id = {o['order_id']: o for o in load_orders(db)}
    assert by_id['B']['amazon_cost_cents'] == 1234


def test_linking_an_email_held_by_another_order_requires_replace(db):
    seed(db, ebay_order('A', created=at(0)), ebay_order('B', created=at(0)))
    with db:
        amazon_email(db, 'm1', at(5))
        manual.link_email(db, 'A', 'm1', NOW)
    with pytest.raises(manual.MatchError, match='already attached'):
        manual.link_email(db, 'B', 'm1', NOW)


def test_accents_and_case_do_not_break_name_or_city_matches():
    result = score({**ORDER, 'first_name': 'Jose', 'city': 'San Jose', 'state': 'CA'},
                   email(first_name='José', city='SAN JOSÉ', state='CA'), CFG)
    assert result['signals']['first_name']['points'] == CFG.first_name_match
    assert result['signals']['city']['points'] == CFG.city_match


def test_old_pending_orders_are_rechecked_when_a_stored_email_is_reparsed(db):
    from backend.services.sync.orchestrator import reparse_emails
    seed(db, ebay_order('A', created=at(0), name='Jordan Doe', city='Spokane', state='WA'))
    later = ORDER_TIME + timedelta(days=5)
    body = ('<div>Seller, thanks for your order!</div><div>1 Automotive item</div>'
            '<div>Jordan - SPOKANE, WA</div><div>Grand Total:</div><div>$27.50</div>')
    with db:
        db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, grand_total_cents,
                      parse_status, body, fetched_at) VALUES ('m1', ?, 'Ordered 1 item: Automotive', 2750,
                      'partial', ?, ?)''', (at(4), body, at(4)))
        assert run_matching(db, CFG, later) == {'PENDING': 1}  # Evaluated while the email was unreadable.
    assert reparse_emails(db, later + timedelta(minutes=1)) == 1
    with db:
        assert run_matching(db, CFG, later + timedelta(minutes=1)) == {'MATCHED': 1}
    assert statuses(db)['A'] == ('MATCHED', 'm1')
