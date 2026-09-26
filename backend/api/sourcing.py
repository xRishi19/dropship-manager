"""Sourcing tab: the saved weekly picks, the live candidate pool, history, and the explicit
(OpenAI-spending) weekly run. Uses the shared DB; no second eBay ingestion."""
import json
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from ..db.connection import connect
from ..integrations.ebay.client import EbayClient
from ..models.schemas import RunBody
from ..services.sourcing.analysis import analyze
from ..services.sourcing.weekly import history as saved_weeks
from ..services.sourcing.weekly import outcomes, resting, week_of
from ..services.sourcing.weekly import weekly
from ..services.sync.orchestrator import refresh_listings
from .deps import get_db

router = APIRouter(tags=['sourcing'])
HEAVY = {'listings', 'candidates', 'config'}


def today():
    return datetime.now(timezone.utc).date().isoformat()


def candidates(request, db):
    """analyze() over the shared DB, cached until the underlying data changes. No OpenAI."""
    key = tuple(db.execute('''SELECT (SELECT max(synced_at) FROM orders), (SELECT count(*) FROM sales),
            (SELECT sum(quantity) FROM sales), (SELECT max(observed_date) FROM snapshots)''').fetchone()) + (today(),)
    cache = request.app.state.candidate_cache
    if cache.get('key') != key:
        diagnostics = {}
        rows, _ = analyze(db, request.app.state.config, today(), diagnostics)
        cache.update(key=key, rows=rows, diagnostics=diagnostics)
    return cache['rows'], cache['diagnostics']


def weeks(db):
    return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM weeks ORDER BY week')]


@router.get('/sourcing/week')
def current_week(week: str | None = None, db=Depends(get_db)):
    saved = weeks(db)
    if not saved:
        return {'week': None, 'results': []}
    payload = next((p for p in saved if p['week'] == week), None) if week else saved[-1]
    if payload is None:
        raise HTTPException(404, 'No saved picks for that week')
    facts = {c['keyword']: c for c in payload.get('candidates', [])}
    rows = []
    for row in payload['results']:
        fact = facts.get(row['keyword'], {})
        rows.append({**row, 'stats': {k: fact.get(k) for k in (
            'sell_through_lift', 'largest_product_share', 'successful_products', 'distinct_successful_listings',
            'distinct_listings', 'unsold_listings', 'recent_units_sold', 'prior_units_sold', 'score',
            'explore_score', 'outside_proven_share', 'examples')}})
    return {**{k: v for k, v in payload.items() if k not in HEAVY}, 'results': rows,
            'current_week': week_of(today()), 'weeks': [p['week'] for p in saved]}


@router.get('/sourcing/candidates')
def candidate_pool(request: Request, db=Depends(get_db)):
    rows, diagnostics = candidates(request, db)
    return {'as_of': today(), 'candidates': rows, 'diagnostics': diagnostics}


@router.get('/sourcing/history')
def keyword_history(request: Request, db=Depends(get_db)):
    config = request.app.state.config
    saved = weeks(db)
    this_week = week_of(today())
    pool, _ = candidates(request, db)
    cooldown = config.tiers.explore_cooldown_weeks
    resting_now = set(resting(pool, saved_weeks(db, this_week), this_week, cooldown))
    # A keyword explored in week W rests through week W+cooldown and is available again after.
    last_explored = {}
    for payload in saved:
        for row in payload['results']:
            if row.get('tier') == 'Explore':
                last_explored[row['keyword']] = payload['week']
    entries = []
    for payload in saved:
        for row in payload['results']:
            cooldown_state = available_from = None
            if row.get('tier') == 'Explore':
                available_from = (date.fromisoformat(payload['week']) + timedelta(weeks=cooldown + 1)).isoformat()
                if payload['week'] >= this_week:
                    cooldown_state = 'picked this week'
                elif last_explored[row['keyword']] != payload['week']:
                    cooldown_state = 'explored again later'
                else:
                    cooldown_state = 'resting' if row['keyword'] in resting_now else 'available'
            entries.append({'week': payload['week'], 'day': row['day'], 'tier': row.get('tier'),
                            'keyword': row['keyword'], 'rank': row['rank'], 'status': row['status'],
                            'total_units_sold': row['total_units_sold'], 'trend': row['trend'],
                            'cooldown': cooldown_state, 'available_from': available_from})
        entries += [{'week': payload['week'], 'keyword': k, 'status': 'DROPPED'} for k in payload.get('dropped', [])]
    # Live results for every saved week (listings started that week whose titles contain the keyword).
    live = outcomes(db, saved, today()) if saved else []
    return {'entries': entries[::-1], 'resting_now': sorted(resting_now), 'outcomes': live,
            'cooldown_weeks': cooldown}


@router.post('/sourcing/weekly/run')
def run_weekly(body: RunBody, request: Request):
    if not body.confirm:
        raise HTTPException(400, 'Confirm the run: it calls the OpenAI API')
    if not os.getenv('OPENAI_API_KEY'):
        raise HTTPException(400, 'OPENAI_API_KEY is not set in .env')
    config = request.app.state.config
    cache = request.app.state.candidate_cache

    def job(state):
        db = connect(config.database, migrate=False)
        api = EbayClient(config.ebay)  # Separate client: the sync loop keeps its own.
        try:
            if body.refresh_listings:
                state['step'] = 'Refreshing eBay listings'
                refresh_listings(db, api, datetime.now(timezone.utc))
                cache.clear()
            state['step'] = 'Analyzing candidates and asking GPT'
            payload = weekly(db, config, today(), refresh=True)
            return {'week': payload['week'], 'selection_method': payload['selection_method']}
        finally:
            api.close()
            db.close()

    return request.app.state.jobs.start('weekly', job)
