"""Weekly history, deterministic status changes and two-per-day scheduling."""
import json
import logging
from datetime import date, timedelta

from .analysis import analyze, coverage
from .keywords import canonical, normalized_words, redundant
from .output import export_analysis, export_week
from .selection import TIER_SIZE, InsufficientCandidates, SelectionError, fallback, select, validate_selection

LOG = logging.getLogger(__name__)
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
HISTORY_WEEKS = 8


def week_of(as_of):
    day = date.fromisoformat(as_of)
    return (day - timedelta(days=day.weekday())).isoformat()


def history(db, week):
    return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM weeks WHERE week<? ORDER BY week', (week,))]


def outcomes(db, earlier, as_of):
    """Listings started during a recommendation week whose titles contain that week's
    keyword, and how they sold. Attribution by title is an approximation."""
    listings = [(r['start_date'], normalized_words(r['title']), r['units']) for r in db.execute(
        '''SELECT l.start_date, l.title, coalesce(sum(s.quantity),0) units FROM listings l
           LEFT JOIN sales s ON s.item_id=l.item_id AND s.sold_date<=?
           WHERE l.start_date IS NOT NULL AND l.start_date<=? GROUP BY l.item_id''', (as_of, as_of))]
    result = []
    for payload in earlier[-HISTORY_WEEKS:]:
        start = payload['week']
        end = (date.fromisoformat(start) + timedelta(days=7)).isoformat()
        started = [(words, units) for day, words, units in listings if start <= day < end]
        keywords = []
        for row in payload['results']:
            needle = normalized_words(row['keyword'])
            matched = [units for words, units in started if needle in words]
            if matched:
                keywords.append({'keyword': row['keyword'], 'tier': row.get('tier'), 'new_listings': len(matched),
                                 'new_listings_sold': sum(u > 0 for u in matched), 'units_sold': sum(matched)})
            else:
                keywords.append({'keyword': row['keyword'], 'tier': row.get('tier'), 'outcome': 'no_data'})
        result.append({'week': start, 'keywords': keywords})
    return result


def resting(candidates, earlier, week, weeks):
    """Explore keywords from the last `weeks` weeks rest while their tests run (results take
    60–90 days). If too few distinct explore candidates remain, the least recently explored
    return first. Proven keywords never rest."""
    cutoff = (date.fromisoformat(week) - timedelta(weeks=weeks)).isoformat()
    last = {}
    for payload in earlier:
        if payload['week'] >= cutoff:
            for row in payload['results']:
                if row.get('tier') == 'Explore':
                    last[canonical(row['keyword'])] = (payload['week'], row['keyword'])
    pool = [c['keyword'] for c in candidates if c.get('tier') == 'explore']

    def distinct(keywords):
        kept = []
        for keyword in keywords:
            if not any(redundant(keyword, k) for k in kept):
                kept.append(keyword)
        return len(kept)

    while last and distinct(k for k in pool if canonical(k) not in last) < TIER_SIZE:
        del last[min(last, key=last.get)]
    return [keyword for _, keyword in sorted(last.values())]


def schedule(selection, candidates, earlier, method='gpt', cooled=(), min_new_category=4):
    # The fallback cannot always meet the new-category minimum, and says so in its market labels.
    selection = validate_selection(selection, candidates, cooled, min_new_category if method == 'gpt' else 0)
    lookup = {c['keyword']: c for c in candidates}
    previous = earlier[-1]['results'] if earlier else []
    previous_keys = {canonical(r['keyword']) for r in previous}
    ever = {canonical(r['keyword']) for p in earlier for r in p['results']}
    rows = []
    for index in range(TIER_SIZE):
        for tier, result in (('Proven', selection.proven[index]), ('Explore', selection.explore[index])):
            facts = lookup[result.keyword]
            key = canonical(result.keyword)
            rows.append({'day': DAYS[index], 'tier': tier, 'market': getattr(result, 'market', None),
                         'keyword': result.keyword, 'rank': index + 1,
                         'confidence': result.confidence if method == 'gpt' else None,
                         'total_units_sold': facts['total_units_sold'],
                         'recent_units_sold': facts['recent_units_sold'], 'distinct_products': facts['distinct_products'],
                         'distinct_successful_listings': facts['distinct_successful_listings'],
                         'sell_through_lift': facts.get('sell_through_lift'),
                         'trend': facts['trend'], 'reason': result.reason,
                         'evidence': 'limited_volume' if facts['total_units_sold'] < 5 else 'at_least_5_units',
                         'status': 'RETAINED' if key in previous_keys else 'RETURNING' if key in ever else 'NEW'})
    current = {canonical(r['keyword']) for r in rows}
    return rows, [r['keyword'] for r in previous if canonical(r['keyword']) not in current]


def weekly(db, config, as_of, refresh=False, selector=select):
    week = week_of(as_of)
    saved = db.execute('SELECT payload FROM weeks WHERE week=?', (week,)).fetchone()
    if saved and not refresh:
        payload = json.loads(saved['payload'])
        export_week(config.output_dir, payload)
        return payload
    diagnostics = {}
    candidates, listings = analyze(db, config, as_of, diagnostics)
    export_analysis(config.output_dir, candidates, listings, diagnostics)
    earlier = history(db, week)
    past = outcomes(db, earlier, as_of)
    tiers = config.tiers
    cooled = resting(candidates, earlier, week, tiers.explore_cooldown_weeks)
    method = 'gpt'
    try:
        selected = selector(candidates, past, model=config.model, cooled=cooled,
                            min_new_category=tiers.explore_min_new_category)
    except InsufficientCandidates:
        raise
    except SelectionError as exc:
        LOG.warning('GPT selection failed (%s); publishing deterministic fallback.', exc)
        selected = fallback(candidates, config.unigram_allowlist, cooled, tiers.explore_min_new_category)
        method = 'fallback'
    rows, dropped = schedule(selected, candidates, earlier, method, cooled, tiers.explore_min_new_category)
    payload = {'week': week, 'as_of': as_of, 'model': config.model, 'selection_method': method,
               'asins': {'Proven': tiers.proven_asins, 'Explore': tiers.explore_asins}, 'resting': cooled,
               'config': config.model_dump(), 'results': rows, 'dropped': dropped,
               'outcomes': past, 'candidates': candidates, 'listings': listings,
               'coverage': coverage(db, as_of), 'diagnostics': diagnostics}
    with db:
        db.execute('INSERT INTO weeks(week,payload) VALUES (?,?) ON CONFLICT(week) DO UPDATE SET payload=excluded.payload',
                   (week, json.dumps(payload)))
    export_week(config.output_dir, payload)
    return payload
