import csv
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.db.ingest import save_rows
from backend.services.sourcing.selection import InsufficientCandidates, Selection, SelectionError, select, validate_selection
from backend.services.sourcing.weekly import outcomes, resting, schedule, weekly

PROVEN = ['jeep wrangler', 'toyota tacoma', 'garage door', 'pressure washer', 'replacement filter',
          'garden hose', 'shower curtain', 'coffee maker', 'vacuum cleaner', 'ceiling fan']
EXPLORE = ['bike rack', 'phone mount', 'dog leash', 'pool pump', 'air fryer', 'sink strainer',
           'yoga mat', 'bird feeder', 'card binder', 'air stone', 'ear pads', 'wrapping paper']
REASON = 'Reusable phrase across several successful products.'


def facts():
    proven = [{'keyword': p, 'tier': 'proven', 'total_units_sold': 20, 'recent_units_sold': 10,
               'distinct_successful_listings': 4, 'distinct_products': 5, 'trend': 'rising',
               'score': 100 - i, 'explore_score': 50 - i, 'outside_proven_share': None} for i, p in enumerate(PROVEN)]
    explore = [{'keyword': p, 'tier': 'explore', 'total_units_sold': 3, 'recent_units_sold': 3,
                'distinct_successful_listings': 2, 'distinct_products': 3, 'trend': 'new_activity',
                'score': 40 - i, 'explore_score': 30 - i,
                'outside_proven_share': .5 if i < 2 else 1.0} for i, p in enumerate(EXPLORE)]
    return proven + explore


def payload(proven=None, explore=None, markets=None):
    explore = explore or EXPLORE[:7]
    markets = markets or ['new_category'] * 4 + ['new_niche'] * (len(explore) - 4)
    return {'proven': [{'keyword': p, 'reason': REASON, 'confidence': .8} for p in (proven or PROVEN[:7])],
            'explore': [{'keyword': p, 'reason': REASON, 'confidence': .7, 'market': m}
                        for p, m in zip(explore, markets)]}


def test_one_proven_and_one_explore_per_day():
    rows, dropped = schedule(validate_selection(payload(), facts()), facts(), [])
    assert len(rows) == 14
    assert set(Counter(r['day'] for r in rows).values()) == {2}
    for day in {r['day'] for r in rows}:
        assert sorted(r['tier'] for r in rows if r['day'] == day) == ['Explore', 'Proven']
    assert [r['keyword'] for r in rows[:4]] == [PROVEN[0], EXPLORE[0], PROVEN[1], EXPLORE[1]]
    assert [r['rank'] for r in rows if r['tier'] == 'Proven'] == list(range(1, 8))
    assert [r['market'] for r in rows if r['tier'] == 'Proven'] == [None] * 7
    assert {r['status'] for r in rows} == {'NEW'}
    assert dropped == []


@pytest.mark.parametrize('count', [6, 11])
def test_wrong_counts_fail(count):
    data = payload()
    data['proven'] = (data['proven'] + data['proven'])[:count]
    with pytest.raises(ValidationError):
        validate_selection(data, facts())


@pytest.mark.parametrize('field,value', [('confidence', 1.1), ('confidence', float('nan')),
    ('confidence', '0.8'), ('keyword', 'invented keyword'), ('supporting_units_sold', 20), ('market', 'other')])
def test_rejects_invalid_invented_or_extra_fields(field, value):
    """GPT returns no statistics; Python facts are joined after validation."""
    data = payload()
    data['explore'][0][field] = value
    with pytest.raises((ValueError, ValidationError)):
        validate_selection(data, facts())


def test_wrong_tier_unknown_and_redundant_picks_fall_through_to_backups():
    candidates = facts() + [dict(facts()[0], keyword='jeep wrangler parts')]
    data = payload(proven=PROVEN[:3] + ['bike rack', 'invented keyword', 'jeep wrangler parts'] + PROVEN[3:7])
    assert [c.keyword for c in validate_selection(data, candidates).proven] == PROVEN[:7]


def test_redundancy_is_checked_across_tiers():
    candidates = facts() + [dict(facts()[-1], keyword='garage door opener')]
    data = payload(explore=['garage door opener'] + EXPLORE[:7], markets=['new_category'] * 8)
    assert [c.keyword for c in validate_selection(data, candidates).explore] == EXPLORE[:7]
    with pytest.raises(SelectionError, match='Redundant selection "garage door opener" overlaps "garage door"'):
        validate_selection(payload(explore=['garage door opener'] + EXPLORE[:6], markets=['new_category'] * 7), candidates)


def test_at_least_four_explore_picks_are_new_categories():
    data = payload(explore=EXPLORE[:10], markets=['new_niche'] * 4 + ['new_category'] * 6)
    kept = validate_selection(data, facts()).explore
    assert Counter(c.market for c in kept) == {'new_niche': 3, 'new_category': 4}
    assert EXPLORE[3] not in [c.keyword for c in kept]
    data = payload(explore=EXPLORE[:10], markets=['new_niche'] * 7 + ['new_category'] * 3)
    with pytest.raises(SelectionError, match='new_niche limit'):
        validate_selection(data, facts())


def test_resting_keyword_rejected_from_explore():
    data = payload(explore=EXPLORE[:8], markets=['new_category'] * 8)
    kept = validate_selection(data, facts(), cooled=['Bike Racks'])
    assert [c.keyword for c in kept.explore] == EXPLORE[1:8]


def test_explore_keywords_rest_for_cooldown_weeks_and_return_when_pool_is_short():
    explored = {'week': '2026-09-14', 'results': [{'keyword': k, 'tier': 'Explore'} for k in EXPLORE[:3]]
                + [{'keyword': PROVEN[0], 'tier': 'Proven'}]}
    assert resting(facts(), [explored], '2026-09-21', 4) == sorted(EXPLORE[:3])
    assert resting(facts(), [dict(explored, week='2026-08-10')], '2026-09-21', 4) == []
    # Twelve explore candidates minus seven resting leaves five: the two oldest return.
    older = {'week': '2026-09-07', 'results': [{'keyword': k, 'tier': 'Explore'} for k in EXPLORE[:2]]}
    newer = {'week': '2026-09-14', 'results': [{'keyword': k, 'tier': 'Explore'} for k in EXPLORE[2:7]]}
    assert resting(facts(), [older, newer], '2026-09-21', 4) == sorted(EXPLORE[2:7])


def test_history_new_retained_returning_dropped():
    earlier = [{'results': [{'keyword': p} for p in PROVEN[:7]]},
               {'results': [{'keyword': p} for p in PROVEN[1:8]]}]
    rows, dropped = schedule(payload(), facts(), earlier)
    assert rows[0]['status'] == 'RETURNING'
    assert rows[2]['status'] == 'RETAINED'
    assert rows[1]['status'] == 'NEW'
    assert dropped == [PROVEN[7]]


def test_sparse_tiers_never_call_model():
    with pytest.raises(InsufficientCandidates, match='Only 6 proven'):
        select(facts()[4:], [], client=object())
    with pytest.raises(InsufficientCandidates, match='5 explore'):
        select(facts(), [], client=object(), cooled=EXPLORE[:7])


def test_model_request_and_retry():
    calls = []
    bad = payload()
    bad['proven'][0]['keyword'] = 'invented keyword'
    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status='completed', output_parsed=Selection.model_validate(bad if len(calls) == 1 else payload()))
    result = select(facts(), [], client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    assert (len(result.proven), len(result.explore)) == (7, 7)
    assert len(calls) == 2
    assert calls[0]['model'] == 'gpt-5.4-mini'
    assert calls[0]['store'] is False
    assert 'At least 4 of the first 7 explore picks' in calls[0]['input'][0]['content']
    assert 'proven_candidates' in calls[0]['input'][1]['content']
    assert 'Unknown candidate "invented keyword"' in calls[1]['input'][-1]['content']


def test_refusal_stops_publication():
    client = SimpleNamespace(responses=SimpleNamespace(parse=lambda **kw: SimpleNamespace(status='completed', output_parsed=None)))
    with pytest.raises(SelectionError, match='refused'):
        select(facts(), [], client=client)


def test_week_saved_and_rerun_does_not_call_model(db, config, monkeypatch):
    monkeypatch.setattr('backend.services.sourcing.weekly.analyze', lambda *args: (facts(), []))
    calls = []
    def selector(*args, **kwargs):
        calls.append(kwargs)
        return Selection.model_validate(payload())
    first = weekly(db, config, '2026-09-23', selector=selector)
    second = weekly(db, config, '2026-09-24', selector=selector)
    assert first == second
    assert len(calls) == 1
    assert calls[0]['min_new_category'] == 4
    with (Path(config.output_dir) / 'weekly_keywords.csv').open() as file:
        rows = list(csv.DictReader(file))
    assert Counter(r['tier'] for r in rows) == {'Proven': 7, 'Explore': 7}
    report = (Path(config.output_dir) / 'weekly_report.md').read_text()
    assert 'pull 200 ASINs' in report and 'pull 50 ASINs' in report


def test_failed_selection_never_persists_week(db, config, monkeypatch):
    monkeypatch.setattr('backend.services.sourcing.weekly.analyze', lambda *args: (facts(), []))
    def selector(*args, **kwargs):
        return payload(proven=PROVEN[:6])
    with pytest.raises(ValidationError):
        weekly(db, config, '2026-09-23', selector=selector)
    assert db.execute('SELECT count(*) FROM weeks').fetchone()[0] == 0


def test_failed_gpt_publishes_marked_deterministic_fallback(db, config, monkeypatch):
    ranked = facts()
    ranked.insert(1, dict(ranked[0], keyword='jeep wrangler parts', score=99.5))
    # Plain single words are skipped without GPT; model codes and allowlisted words stay.
    ranked.insert(0, dict(ranked[0], keyword='plug', score=103))
    ranked.insert(0, dict(ranked[0], keyword='oem', score=102))
    ranked.insert(0, dict(ranked[0], keyword='f150', score=101))
    monkeypatch.setattr('backend.services.sourcing.weekly.analyze', lambda *args: (ranked, []))
    def selector(*args, **kwargs):
        raise SelectionError('GPT output failed validation after 3 attempts')
    result = weekly(db, config, '2026-09-23', selector=selector)
    assert result['selection_method'] == 'fallback'
    proven = [r['keyword'] for r in result['results'] if r['tier'] == 'Proven']
    explore = [(r['keyword'], r['market']) for r in result['results'] if r['tier'] == 'Explore']
    assert proven == ['oem', 'f150'] + PROVEN[:5]
    # Four listings-never-mention-proven picks first, then the best remaining by explore score.
    assert explore == [(k, 'new_category') for k in EXPLORE[2:6]] + [
        (EXPLORE[0], 'new_niche'), (EXPLORE[1], 'new_niche'), (EXPLORE[6], 'new_category')]
    assert {r['confidence'] for r in result['results']} == {None}
    assert 'GPT selection failed' in (Path(config.output_dir) / 'weekly_report.md').read_text()


def test_sparse_candidates_never_fall_back(db, config, monkeypatch):
    monkeypatch.setattr('backend.services.sourcing.weekly.analyze', lambda *args: (facts()[4:], []))
    with pytest.raises(SelectionError, match='Only 6 proven'):
        weekly(db, config, '2026-09-23')
    assert db.execute('SELECT count(*) FROM weeks').fetchone()[0] == 0


def test_outcomes_attribute_new_listings_to_recommendation_week(db):
    listing = lambda item, title, start: {'item_id': item, 'title': title, 'sku': '', 'sold_quantity': 0,
        'observed_date': '2026-09-20', 'status': 'active', 'start_date': start, 'end_date': None}
    with db:
        save_rows(db, [listing('1', 'Garage Door Openers remote', '2026-09-08'),
                       listing('2', 'garage door cable', '2026-09-09'),
                       listing('3', 'garage door spring', '2026-09-01')], 'listings')
        save_rows(db, [{'item_id': '1', 'title': 'Garage Door Openers remote', 'sku': '', 'order_id': 'o1',
                        'sold_date': '2026-09-12', 'quantity': 3}], 'orders')
    earlier = [{'week': '2026-09-07', 'results': [{'keyword': 'garage door opener', 'tier': 'Explore'},
                                                  {'keyword': 'garage door', 'tier': 'Proven'},
                                                  {'keyword': 'pool pump'}]}]
    [week] = outcomes(db, earlier, '2026-09-23')
    assert week['keywords'] == [
        {'keyword': 'garage door opener', 'tier': 'Explore', 'new_listings': 1, 'new_listings_sold': 1, 'units_sold': 3},
        {'keyword': 'garage door', 'tier': 'Proven', 'new_listings': 2, 'new_listings_sold': 1, 'units_sold': 3},
        {'keyword': 'pool pump', 'tier': None, 'outcome': 'no_data'}]
