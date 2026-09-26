import math

import pytest

from backend.services.sourcing.analysis import analyze, listing_metrics, trend
from backend.db.ingest import save_rows
from backend.services.sourcing.keywords import extract, canonical, redundant


def sale(item, title, quantity, sold_date='2026-09-15', sku='', order=None):
    return {'item_id': item, 'title': title, 'quantity': quantity, 'sold_date': sold_date,
            'sku': sku, 'order_id': order or item + sold_date}


def test_exact_window_boundaries_future_and_unsold(db):
    with db:
        save_rows(db, [sale('1', 'garage door cable', 2, '2026-09-01'),
                       sale('1', 'garage door cable', 3, '2026-09-08'),
                       sale('1', 'garage door cable', 4, '2026-09-14'),
                       sale('1', 'garage door cable', 100, '2026-09-15')], 'orders')
        save_rows(db, [{'item_id': '2', 'title': 'garage door bracket', 'sku': '',
                       'observed_date': '2026-09-14', 'sold_quantity': 0, 'status': 'active'}], 'listings')
    rows = listing_metrics(db, '2026-09-14', 7)
    assert (rows[0]['total_units_sold'], rows[0]['recent_units_sold'], rows[0]['prior_units_sold']) == (9, 7, 2)
    assert rows[1]['total_units_sold'] == 0


def test_viral_sku_excluded_even_across_relistings(db, config):
    with db:
        save_rows(db, [sale('1', 'Jeep Wrangler floor mats', 50, sku='same'),
                       sale('2', 'Jeep Wrangler seat cover', 50, sku='same'),
                       sale('3', 'Jeep Wrangler door handle', 1, sku='other')], 'orders')
    candidates, _ = analyze(db, config, '2026-09-23')
    assert not candidates


def test_units_breadth_mean_median_unsold_and_no_money(db, config):
    with db:
        save_rows(db, [sale('1', 'Jeep Wrangler floor mats', 10, sku='a'),
                       sale('2', 'Jeep Wrangler seat cover', 6, sku='b')], 'orders')
        save_rows(db, [{'item_id': '3', 'title': 'Jeep Wrangler door handle', 'sku': 'c',
                       'observed_date': '2026-09-01', 'sold_quantity': 0, 'status': 'active'}], 'listings')
    candidates, _ = analyze(db, config, '2026-09-23')
    item = next(c for c in candidates if c['keyword'] == 'jeep wrangler')
    assert item['total_units_sold'] == 16
    assert item['distinct_successful_listings'] == 2
    assert item['unsold_listings'] == 1
    assert item['distinct_products'] == 3
    assert item['median_units_sold'] == 6
    assert item['mean_units_sold'] == 5.3333
    assert 'revenue' not in item


def test_literal_extraction_no_stopword_or_number_bridging():
    words = extract('New OEM replacement filter for Toyota Tacoma AB-123 2020 garage door opener')
    assert {'oem', 'replacement filter', 'toyota tacoma', 'garage door'} <= words
    assert not any('123' in w or '2020' in w or 'for' in w for w in words)
    assert 'filter toyota' not in words
    assert 'tacoma garage' not in words
    assert 'replacement' not in words
    assert 'garage door' not in extract('garage door')


def test_redundancy():
    assert canonical('Replacement Filters') == canonical('filter replacement')
    assert redundant('garage door', 'garage door opener')
    assert redundant('replacement filters', 'replacement filter')
    assert not redundant('Jeep Wrangler', 'Toyota Tacoma')


def test_trend():
    assert trend(0, 0) == 'flat'
    assert trend(4, 0) == 'new_activity'
    assert trend(12, 10) == 'rising'
    assert trend(8, 10) == 'falling'
    assert trend(11, 10) == 'flat'


def test_low_volume_repeated_products_qualify_with_transparent_diagnostics(db, config):
    with db:
        save_rows(db, [sale('1', 'Toyota Camry floor mats', 1, sku='a'),
                       sale('2', 'Toyota Camry seat cover', 1, sku='b')], 'orders')
    diagnostics = {}
    candidates, _ = analyze(db, config, '2026-09-23', diagnostics)
    candidate = next(c for c in candidates if c['keyword'] == 'toyota camry')
    assert candidate['total_units_sold'] == 2
    assert candidate['distinct_successful_listings'] == 2
    assert candidate['successful_products'] == 2
    assert candidate['evidence'] == 'limited_volume'
    assert diagnostics['funnel']['selected_candidates'] == len(candidates)
    assert diagnostics['limited_volume_candidates'] == len(candidates)
    strict = config.model_copy(update={'min_total_units': 5})
    strict_diagnostics = {}
    assert analyze(db, strict, '2026-09-23', strict_diagnostics)[0] == []
    assert strict_diagnostics['funnel']['after_concentration'] > 0
    assert strict_diagnostics['funnel']['after_minimum_units'] == 0


def test_two_units_on_one_product_still_fail(db, config):
    with db:
        save_rows(db, [sale('1', 'Toyota Camry floor mats', 2, sku='a')], 'orders')
    assert analyze(db, config, '2026-09-23')[0] == []


def test_common_hitch_synonyms_cannot_consume_two_slots():
    assert canonical('tow hitch') == canonical('trailer hitch')
    assert redundant('tow hitch', 'trailer hitch')


def test_configured_incomplete_phrases_are_excluded():
    phrases = extract('OEM iridium spark plug direct replacement', blocked=['iridium spark', 'direct replacement'])
    assert 'iridium spark' not in phrases
    assert 'direct replacement' not in phrases
    assert 'spark plug' in phrases


def listing(item, title, sku='', start=None, end=None, observed='2026-09-20', status='active'):
    return {'item_id': item, 'title': title, 'sku': sku, 'sold_quantity': 0, 'observed_date': observed,
            'status': status, 'start_date': start, 'end_date': end}


def test_model_codes_kept_merged_and_part_numbers_rejected():
    codes = {'f150', 'rav4', '4runner', 'crv'}
    words = extract('Mud Flaps for Ford F-150 RAV4 4Runner CR-V 2pcs M12x1.25 90919-22386 UF311', codes=codes,
                    unigram_allowlist=['cr-v'])
    assert {'ford f-150', 'f-150', 'rav4', '4runner', 'cr-v', 'mud flaps'} <= words
    assert not any(w in words for w in ['2pcs', 'm12x1.25', '90919-22386', 'uf311'])
    assert canonical('Ford F-150') == canonical('ford f150')
    # Model codes are only usable when the catalog shows them across several products.
    assert 'rav4' not in extract('Mud Flaps RAV4 splash guard')


def test_phrases_never_cross_separators_or_truncated_words():
    words = extract('KADRICK Lug Nuts | Heavy Duty Chrome, Wheel Studs for Nissan Muran...')
    assert {'lug nuts', 'heavy duty', 'wheel studs'} <= words
    assert 'nuts heavy' not in words and 'chrome wheel' not in words
    assert not any('muran' in w for w in words)


def test_single_words_limited_to_model_codes_and_curated_list():
    words = extract('Toyota Tacoma F150 brown seat cover', codes={'f150'}, unigram_allowlist=['oem', 'tacoma'])
    assert {'tacoma', 'f150', 'toyota tacoma', 'seat cover'} <= words
    assert not {'toyota', 'brown', 'seat', 'cover'} & words


def test_rare_model_codes_are_treated_as_part_numbers(db, config):
    rows = []
    for index, product in enumerate(['floor mats', 'seat cover', 'mud flaps', 'tail light', 'door handle', 'sun visor']):
        rows.append(sale(str(index), f'F150 {product}', 2, sku=f'f{index}'))
    rows += [sale('20', 'UF311 ignition coil pack', 1, sku='u1'), sale('21', 'UF311 ignition coil boot', 1, sku='u2')]
    with db:
        save_rows(db, rows, 'orders')
    keywords = {c['keyword'] for c in analyze(db, config, '2026-09-23')[0]}
    assert 'f150' in keywords
    assert not any('uf311' in k for k in keywords)


def test_curated_model_names_qualify_but_generic_words_do_not(db, config):
    curated = config.model_copy(update={'unigram_allowlist': ['oem', 'tacoma']})
    with db:
        save_rows(db, [sale('1', 'Tacoma floor mats', 2, sku='a'), sale('2', 'Tacoma seat cover', 2, sku='b'),
                       sale('3', 'Camry floor mats', 2, sku='c')], 'orders')
    keywords = {c['keyword'] for c in analyze(db, curated, '2026-09-23')[0]}
    assert 'tacoma' in keywords and 'floor mats' in keywords
    assert not {'mats', 'floor', 'camry'} & keywords


def test_below_store_average_sell_through_is_gated_and_lift_ranks(db, config):
    nouns = ['bracket', 'cable', 'spring', 'roller']
    rows = [sale(str(i), f'garage door {noun}', 1, sku=f'g{i}') for i, noun in enumerate(nouns)]
    rows += [sale(str(10 + i), f'pool pump {noun}', 1, sku=f'p{i}') for i, noun in enumerate(['motor', 'seal', 'basket', 'lid'])]
    with db:
        save_rows(db, rows, 'orders')
        # 60 unsold "pool pump" listings dilute its sell-through below the store average.
        save_rows(db, [listing(str(100 + i), f'pool pump model{i} hose', sku=f'u{i}') for i in range(60)], 'listings')
    diagnostics = {}
    candidates = analyze(db, config, '2026-09-23', diagnostics)[0]
    by_keyword = {c['keyword']: c for c in candidates}
    assert by_keyword['garage door']['sell_through_lift'] > 1
    assert 'pool pump' not in by_keyword
    assert diagnostics['funnel']['after_minimum_units'] > diagnostics['funnel']['after_lift']
    loose = config.model_copy(update={'min_lift': 0})
    ranked = [c['keyword'] for c in analyze(db, loose, '2026-09-23')[0]]
    assert ranked.index('garage door') < ranked.index('pool pump')


def test_exposure_days_use_listing_dates(db):
    with db:
        save_rows(db, [listing('1', 'garage door cable', start='2026-09-01', end='2026-09-10', status='ended_unsold'),
                       listing('2', 'garage door spring', start='2026-01-01'),
                       listing('3', 'garage door opener')], 'listings')
    rows = {r['item_id']: r for r in listing_metrics(db, '2026-09-20', 7, 90)}
    assert (rows['1']['exposure_days'], rows['2']['exposure_days'], rows['3']['exposure_days']) == (10, 90, 90)
    # Listings started after --as-of are excluded, enabling backtests.
    assert '1' not in {r['item_id'] for r in listing_metrics(db, '2026-08-31', 7, 90)}


def test_trend_normalizes_catalog_growth_and_small_counts():
    assert trend(2, 1, 100, 50, min_units=4) == 'insufficient_data'
    assert trend(20, 10, 200, 100, min_units=4) == 'flat'
    assert trend(20, 10, 100, 100, min_units=4) == 'rising'
    assert trend(10, 10, 200, 100, min_units=4) == 'falling'


def test_different_products_that_sell_together_both_stay(db, config):
    rows = [sale('1', 'Spark Plug set', 3, sku='a'), sale('2', 'Spark Plug with Ignition Coil', 1, sku='b'),
            sale('3', 'Spark Plug and Ignition Coil', 1, sku='c'), sale('4', 'Spark Plug gapper', 2, sku='d')]
    with db:
        save_rows(db, rows, 'orders')
        save_rows(db, [listing('5', 'Ignition Coil bulk', sku='e')]
                  + [listing(str(10 + i), f'garden hose model{i} reel', sku=f'h{i}') for i in range(10)], 'listings')
    keywords = {c['keyword'] for c in analyze(db, config, '2026-09-23')[0]}
    # Every "ignition coil" sale is also a "spark plug" sale, but they reach different listings.
    assert {'spark plug', 'ignition coil'} <= keywords


def test_nested_phrase_on_identical_listings_keeps_complete_concept(db, config):
    rows = [sale(str(i), f'Spark Plug {name}', 2, sku=f's{i}') for i, name in enumerate(['set', 'gapper', 'socket'])]
    with db:
        save_rows(db, rows, 'orders')
        save_rows(db, [listing(str(10 + i), f'garden hose model{i} reel', sku=f'h{i}') for i in range(10)], 'listings')
    keywords = {c['keyword'] for c in analyze(db, config, '2026-09-23')[0]}
    assert 'spark plug' in keywords
    assert not {'spark', 'plug'} & keywords


TIER_PHRASES = ['garage door', 'pool pump', 'bike rack', 'dog leash', 'coffee maker', 'ceiling fan', 'phone mount']


def tier_rows():
    rows = [sale(f'{i}-{j}', f'{phrase} {noun}', 2, sku=f'{i}{j}')
            for i, phrase in enumerate(TIER_PHRASES) for j, noun in enumerate(['cable', 'motor', 'bracket'])]
    return rows + [sale('y1', 'yoga mat thick', 1, sku='y1'), sale('y2', 'yoga mat strap', 1, sku='y2'),
                   sale('s1', 'shower curtain liner', 1, sku='s1'), sale('s2', 'shower curtain garage door hook', 1, sku='s2')]


def test_proven_explore_tiers_scores_and_new_market_share(db, config):
    with db:
        save_rows(db, tier_rows(), 'orders')
    diagnostics = {}
    loose = config.model_copy(update={'min_lift': 0})
    by_keyword = {c['keyword']: c for c in analyze(db, loose, '2026-09-23', diagnostics)[0]}
    assert {k for k, c in by_keyword.items() if c['tier'] == 'proven'} == set(TIER_PHRASES)
    assert by_keyword['yoga mat']['tier'] == by_keyword['shower curtain']['tier'] == 'explore'
    assert diagnostics['tiers']['promoted_to_proven'] == []
    # Explore ranking is the main score without the total-volume term.
    mat = by_keyword['yoga mat']
    assert mat['score'] - mat['explore_score'] == pytest.approx(loose.weights.units * math.log1p(2), abs=1e-5)
    # Yoga mat listings never mention a proven keyword; one of two shower curtain listings does.
    assert (mat['outside_proven_share'], by_keyword['shower curtain']['outside_proven_share']) == (1.0, .5)
    assert by_keyword['garage door']['outside_proven_share'] is None


def test_strongest_explore_candidates_promoted_when_proven_is_short(db, config):
    with db:
        save_rows(db, tier_rows(), 'orders')
    strict = config.model_copy(update={'min_lift': 0, 'tiers': config.tiers.model_copy(update={'proven_min_units': 7})})
    diagnostics = {}
    by_keyword = {c['keyword']: c for c in analyze(db, strict, '2026-09-23', diagnostics)[0]}
    # Only "garage door" (7 units) qualifies; the six next-best become Proven to fill 7 days.
    assert sorted(diagnostics['tiers']['promoted_to_proven']) == sorted(TIER_PHRASES[1:])
    assert by_keyword['yoga mat']['tier'] == 'explore'
