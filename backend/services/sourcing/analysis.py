"""All factual aggregation and scoring happens here, never in GPT."""
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, timedelta

from .keywords import canonical, extract, model_codes, normalized_words, redundant
from .selection import TIER_SIZE


def listing_metrics(db, as_of, recent_days, window_days=90):
    end = date.fromisoformat(as_of)
    window_start = end - timedelta(days=window_days - 1)
    recent_start = (end - timedelta(days=recent_days - 1)).isoformat()
    prior_start = (end - timedelta(days=2 * recent_days - 1)).isoformat()
    records = db.execute('''SELECT l.*,
        coalesce(sum(s.quantity),0) total_units_sold,
        coalesce(sum(CASE WHEN s.sold_date>=? THEN s.quantity ELSE 0 END),0) recent_units_sold,
        coalesce(sum(CASE WHEN s.sold_date>=? AND s.sold_date<? THEN s.quantity ELSE 0 END),0) prior_units_sold
        FROM listings l LEFT JOIN sales s ON l.item_id=s.item_id AND s.sold_date<=? AND s.sold_date>=?
        WHERE coalesce(l.start_date,l.first_seen)<=?
        -- A listing known only from the accounting history backfill (sales tagged
        -- 'ebay_backfill', none in the window) must not join the analysis as a zero-sale
        -- listing. Everything the sourcing sync/imports know about is kept exactly as before.
        AND (EXISTS (SELECT 1 FROM snapshots n WHERE n.item_id=l.item_id)
             OR EXISTS (SELECT 1 FROM sales w WHERE w.item_id=l.item_id AND (w.source<>'ebay_backfill'
                        OR (w.sold_date>=? AND w.sold_date<=?))))
        GROUP BY l.item_id''',
        (recent_start, prior_start, recent_start, as_of, window_start.isoformat(), as_of,
         window_start.isoformat(), as_of))
    result = [dict(row) for row in records]
    for row in result:
        row['trend'] = trend(row['recent_units_sold'], row['prior_units_sold'])
        row['exposure_days'] = exposure_days(row, window_start, end)
    return result


def exposure_days(row, window_start, end):
    """Days live inside the analysis window; missing dates assume the whole window."""
    start = max(window_start, date.fromisoformat(row['start_date'])) if row.get('start_date') else window_start
    stop = min(end, date.fromisoformat(row['end_date'])) if row.get('end_date') else end
    return max(1, (stop - start).days + 1)


def trend(recent, prior, store_recent=0, store_prior=0, min_units=0):
    """Compare recent vs prior units; with store totals, compare shares of store units
    so catalog growth alone does not read as a rising keyword."""
    if recent + prior < min_units:
        return 'insufficient_data'
    if store_recent and store_prior:
        recent, prior = recent / store_recent, prior / store_prior
    if prior == 0:
        return 'new_activity' if recent else 'flat'
    change = (recent - prior) / prior
    return 'rising' if change >= .2 else 'falling' if change <= -.2 else 'flat'


def analyze(db, config, as_of, diagnostics=None):
    listings = listing_metrics(db, as_of, config.recent_days, config.analysis_days)
    # Model codes (F150, RAV4) qualify only when shared by several products; part numbers aren't.
    code_products = defaultdict(set)
    for listing in listings:
        for code in model_codes(listing['title']):
            code_products[code].add(listing['product_key'])
    codes = {code for code, products in code_products.items() if len(products) >= config.min_alnum_token_products}
    # Group plural/reordered/hyphen variants; show the most common literal form.
    associations, surfaces = defaultdict(dict), defaultdict(Counter)
    for listing in listings:
        for phrase in extract(listing['title'], config.blocked_phrases, codes, config.unigram_allowlist):
            key = canonical(phrase)
            associations[key][listing['item_id']] = listing
            surfaces[key][phrase] += 1
    store_units = sum(r['total_units_sold'] for r in listings)
    store_exposure = sum(r['exposure_days'] for r in listings)
    store_recent = sum(r['recent_units_sold'] for r in listings)
    store_prior = sum(r['prior_units_sold'] for r in listings)
    base_rate = store_units / store_exposure if store_exposure else 0
    candidates = []
    funnel = dict.fromkeys(['extracted_phrases', 'with_sales', 'after_successful_listings',
                           'after_successful_products', 'after_concentration',
                           'after_minimum_units', 'after_lift'], 0)
    w = config.weights
    for key, by_item in associations.items():
        matched = list(by_item.values())
        keyword = min(surfaces[key].items(), key=lambda kv: (-kv[1], kv[0]))[0]
        funnel['extracted_phrases'] += 1
        units = sum(r['total_units_sold'] for r in matched)
        successful = [r for r in matched if r['total_units_sold'] > 0]
        products = defaultdict(int)
        for row in matched:
            products[row['product_key']] += row['total_units_sold']
        successful_products = sum(q > 0 for q in products.values())
        share = max(products.values()) / units if units else 1
        if units == 0:
            continue
        funnel['with_sales'] += 1
        if len(successful) < config.min_successful_listings:
            continue
        funnel['after_successful_listings'] += 1
        if successful_products < config.min_successful_products:
            continue
        funnel['after_successful_products'] += 1
        if share > config.max_product_share:
            continue
        funnel['after_concentration'] += 1
        if units < config.min_total_units:
            continue
        funnel['after_minimum_units'] += 1
        # Smoothed units per listing-day relative to the whole store (1.0 = store average).
        exposure = sum(r['exposure_days'] for r in matched)
        alpha = config.lift_prior_units
        lift = (units + alpha) / (exposure + alpha / base_rate) / base_rate
        if lift < config.min_lift - 1e-9:
            continue
        funnel['after_lift'] += 1
        recent = sum(r['recent_units_sold'] for r in matched)
        prior = sum(r['prior_units_sold'] for r in matched)
        # Explore ranking drops the total-volume term: sell-through and recency lead.
        explore_score = (w.recent_units * math.log1p(recent) + w.breadth * math.log1p(successful_products)
                         + w.lift * math.log(lift) - w.concentration * share)
        score = w.units * math.log1p(units) + explore_score
        candidates.append({'keyword': keyword, 'total_units_sold': units,
            'evidence': 'limited_volume' if units < 5 else 'at_least_5_units',
            'recent_units_sold': recent, 'prior_units_sold': prior,
            'distinct_successful_listings': len(successful), 'unsold_listings': len(matched) - len(successful),
            'distinct_listings': len(matched), 'distinct_products': len(products),
            'successful_products': successful_products,
            'mean_units_sold': round(statistics.mean(r['total_units_sold'] for r in matched), 4),
            'median_units_sold': statistics.median(r['total_units_sold'] for r in matched),
            'success_rate': round(len(successful) / len(matched), 4),
            'exposure_days': exposure, 'sell_through_lift': round(lift, 3),
            'largest_product_share': round(share, 4),
            'trend': trend(recent, prior, store_recent, store_prior, config.min_trend_units),
            'score': round(score, 6), 'explore_score': round(explore_score, 6),
            'examples': [{'title': r['title'], 'units': r['total_units_sold']} for r in sorted(matched, key=lambda r: (-r['total_units_sold'], r['item_id']))[:5]],
            '_items': set(by_item)})
    # On equal scores prefer the more complete phrase over a single-word fragment.
    candidates.sort(key=lambda c: (-c['score'], -len(c['keyword'].split()), c['keyword']))
    kept = []
    for candidate in candidates:
        # Nested phrases on identical listings: keep the longer, complete concept
        # ("spark plug", not the fragment "plug"), in the shorter one's rank position.
        twin = next((i for i, c in enumerate(kept) if candidate['_items'] == c['_items']
                     and redundant(candidate['keyword'], c['keyword'])), None)
        if twin is not None:
            if len(candidate['keyword'].split()) > len(kept[twin]['keyword'].split()):
                kept[twin] = candidate
            continue
        # Different products that sell together (ignition coil, spark plugs) both stay:
        # each keyword reaches different Amazon listings.
        kept.append(candidate)
    funnel['after_deduplication'] = len(kept)
    kept = kept[:config.candidate_limit]
    funnel['selected_candidates'] = len(kept)
    promoted = assign_tiers(kept, config, {r['item_id']: r['title'] for r in listings})
    for candidate in kept:
        candidate.pop('_items')
    if diagnostics is not None:
        diagnostics.update({'as_of': as_of, 'funnel': funnel,
            'thresholds': {'min_total_units': config.min_total_units,
                'min_successful_listings': config.min_successful_listings,
                'min_successful_products': config.min_successful_products,
                'max_product_share': config.max_product_share, 'min_lift': config.min_lift},
            'analysis_days': config.analysis_days,
            'store_base_units_per_listing_day': round(base_rate, 6),
            'model_codes_allowed': len(codes),
            'tiers': {'proven': sum(c['tier'] == 'proven' for c in kept),
                      'explore': sum(c['tier'] == 'explore' for c in kept),
                      'proven_min_units': config.tiers.proven_min_units,
                      'promoted_to_proven': promoted},
            'limited_volume_candidates': sum(c['total_units_sold'] < 5 for c in kept),
            'note': 'Limited volume means 2–4 observed units, not statistically proven demand. No candidates are padded.'})
    return kept, listings


def assign_tiers(candidates, config, titles):
    """Proven = at least `proven_min_units`; the rest explore. If fewer than 7 distinct
    proven keywords exist, the highest-scoring explore candidates are promoted."""
    for candidate in candidates:
        candidate['tier'] = 'proven' if candidate['total_units_sold'] >= config.tiers.proven_min_units else 'explore'
    distinct = [c['keyword'] for c in candidates if c['tier'] == 'proven']
    distinct = [k for i, k in enumerate(distinct) if not any(redundant(k, other) for other in distinct[:i])]
    promoted = []
    for candidate in candidates:  # Score order.
        if len(distinct) >= TIER_SIZE:
            break
        if candidate['tier'] == 'explore' and not any(redundant(candidate['keyword'], k) for k in distinct):
            candidate['tier'] = 'proven'
            distinct.append(candidate['keyword'])
            promoted.append(candidate['keyword'])
    # Share of an explore keyword's listings mentioning no proven keyword: a hint that it
    # is a different market. GPT judges the actual category.
    proven = [normalized_words(c['keyword']) for c in candidates if c['tier'] == 'proven']
    normalized = {}
    for candidate in candidates:
        if candidate['tier'] == 'proven':
            candidate['outside_proven_share'] = None
            continue
        outside = 0
        for item in candidate['_items']:
            if item not in normalized:
                normalized[item] = normalized_words(titles[item])
            outside += not any(p in normalized[item] for p in proven)
        candidate['outside_proven_share'] = round(outside / len(candidate['_items']), 3)
    return promoted


def coverage(db, as_of):
    span = db.execute('SELECT min(sold_date),max(sold_date) FROM sales WHERE sold_date<=?', (as_of,)).fetchone()
    syncs = [dict(r) for r in db.execute('SELECT start_date,end_date,completed_at FROM syncs ORDER BY id')]
    dated = db.execute('SELECT count(*) FROM listings WHERE start_date IS NOT NULL').fetchone()[0]
    return {'observed_sales_start': span[0], 'observed_sales_end': span[1], 'api_syncs': syncs,
            'listings_with_start_date': dated,
            'quantity_basis': 'Gross order units excluding fully canceled orders; not net returns.',
            'limitations': 'Zero sales means no measured sales in the analysis window. CSV date coverage is not verified. API listing discovery includes active listings and the last 60 days of ended unsold listings. Snapshot counters are audit-only and never added to order quantities. Partial cancellations and returns are not deducted. Today may be partial. Product identity uses the Amazon ASIN decoded from the SKU where available, otherwise parent SKU or normalized title. Listings without start/end dates are assumed live for the whole window.'}
