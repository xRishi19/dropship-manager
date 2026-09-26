"""One synchronization cycle: eBay orders, finances, returns, categories, listings, Gmail and
reconciliation. Business logic only; the local poll loop (loop.py) or a future hosted job /
eBay webhook can call run_cycle() unchanged. Never calls OpenAI.

Each step records its own state in sync_state, so one failing step (e.g. Gmail not yet
connected) never blocks the others. The accounting backfill from accounting.start_date runs
in resumable chunks across cycles.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from ...db.connection import connect
from ...integrations.ebay.client import EbayClient
from ...integrations.gmail.amazon_parser import parse, redact
from ...integrations.gmail.client import GmailClient, GmailNotConfigured
from ..reconciliation.matcher import run_matching
from .ebay_sync import stamp, store_categories, store_listings, store_orders, store_returns, store_transactions

LOG = logging.getLogger(__name__)
BACKFILL_CHUNKS_PER_CYCLE = 3
# Gmail limits "query units per minute per user", and a full message fetch costs several units.
# Fetch a modest, paced batch per cycle and stop quietly on a quota error; the next cycle resumes.
GMAIL_MESSAGES_PER_CYCLE = 60
GMAIL_FETCH_PAUSE_SECONDS = 0.25


class Skipped(Exception):
    """A step that intentionally did nothing this cycle (not configured, not due)."""


def get_state(db, name):
    row = db.execute('SELECT * FROM sync_state WHERE name=?', (name,)).fetchone()
    return dict(row) if row else {}


def save_state(db, name, now, *, success=None, error=None, cursor=None, details=None):
    state = get_state(db, name) or {'name': name}
    state['last_attempt_at'] = stamp(now)
    if success:
        state['last_success_at'], state['last_error'] = stamp(now), None
    if error is not None:
        state['last_error'] = error
    if cursor is not None:
        state['cursor'] = cursor
    if details is not None:
        state['details_json'] = json.dumps(details)
    columns = ['name', 'last_success_at', 'cursor', 'last_attempt_at', 'last_error', 'details_json']
    with db:
        db.execute(f'''INSERT INTO sync_state({",".join(columns)}) VALUES ({",".join("?" * len(columns))})
                       ON CONFLICT(name) DO UPDATE SET {", ".join(f"{c}=excluded.{c}" for c in columns[1:])}''',
                   [state.get(c) for c in columns])


def parse_stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def accounting_start(config):
    return datetime.fromisoformat(config.accounting.start_date).replace(tzinfo=timezone.utc)


class SyncContext:
    """Long-lived clients for the loop (keeps the eBay OAuth token between cycles)."""

    def __init__(self, config, ebay=None, gmail=None):
        self.config = config
        self._ebay = ebay
        self._gmail = gmail
        self.current_step = None  # Shown in the UI while a cycle runs.

    @property
    def ebay(self):
        if self._ebay is None:
            self._ebay = EbayClient(self.config.ebay)
        return self._ebay

    @property
    def gmail(self):
        if self._gmail is None:
            if not self.config.gmail.enabled:
                raise Skipped('Gmail disabled in config')
            try:
                self._gmail = GmailClient(self.config.gmail.token_path)
            except GmailNotConfigured as exc:
                raise Skipped(str(exc)) from None
        return self._gmail


def backfill(db, name, start, now, chunk_days, fetch, store):
    """Advance a resumable creation/transaction-date backfill by a few chunks. Once it
    reaches the present it is marked complete; incremental sync takes over."""
    saved = get_state(db, name).get('cursor')
    if saved == 'complete':
        return {'backfill_complete': True}
    cursor = parse_stamp(saved) or start
    done = 0
    for _ in range(BACKFILL_CHUNKS_PER_CYCLE):
        if cursor >= now:
            break
        end = min(cursor + timedelta(days=chunk_days), now)
        rows = fetch(stamp(cursor), stamp(end))
        with db:
            store(rows)
        done += len(rows)
        cursor = end
        save_state(db, name, now, success=True, cursor='complete' if cursor >= now else stamp(cursor),
                   details={'rows': done})
    return {'backfilled': done, 'backfill_complete': cursor >= now}


def step_orders(db, ctx, now):
    """Live sync: orders created or changed since the last success (with overlap)."""
    since = parse_stamp(get_state(db, 'orders').get('last_success_at'))
    since = (since - timedelta(minutes=ctx.config.sync.order_overlap_minutes)) if since else now - timedelta(days=2)
    changed = ctx.ebay.raw_orders(stamp(since), stamp(now), 'lastmodifieddate')
    with db:
        store_orders(db, changed, now)
    return {'changed': len(changed)}


def step_orders_backfill(db, ctx, now):
    """History since accounting.start_date, a few chunks per cycle. A separate step so a
    failing chunk never blocks live order sync. Sales it adds are tagged 'ebay_backfill'."""
    config = ctx.config
    return backfill(db, 'orders_backfill', accounting_start(config), now, config.sync.backfill_chunk_days,
                    lambda a, b: ctx.ebay.raw_orders(a, b),
                    lambda rows: store_orders(db, rows, now, source='ebay_backfill'))


def require_finances_scope(ctx):
    ctx.ebay.access_token()
    if ctx.ebay.finances_scope_missing:
        raise RuntimeError('eBay consent lacks the Finances scope; run python ebay_auth.py again. '
                           'Revenue is shown as a fee estimate until then.')


def step_finances(db, ctx, now):
    require_finances_scope(ctx)
    since = parse_stamp(get_state(db, 'finances').get('last_success_at'))
    since = (since - timedelta(days=2)) if since else now - timedelta(days=2)  # Late postings.
    rows = ctx.ebay.finance_transactions(stamp(since), stamp(now))
    with db:
        updated = store_transactions(db, rows, now)
    return {'transactions': len(rows), 'orders_updated': updated}


def step_finances_backfill(db, ctx, now):
    require_finances_scope(ctx)
    config = ctx.config
    return backfill(db, 'finances_backfill', accounting_start(config), now, config.sync.backfill_chunk_days,
                    ctx.ebay.finance_transactions, lambda rows: store_transactions(db, rows, now))


def due(db, name, now, interval):
    last = parse_stamp(get_state(db, name).get('last_success_at'))
    if last and now - last < interval:
        raise Skipped('not due')


def step_returns(db, ctx, now):
    due(db, 'returns', now, timedelta(minutes=ctx.config.sync.returns_interval_minutes))
    members = ctx.ebay.returns(stamp(accounting_start(ctx.config)), stamp(now))
    with db:
        return {'returns': store_returns(db, members, now)}


def step_categories(db, ctx, now):
    return {'looked_up': store_categories(db, ctx.ebay)}


def step_listings(db, ctx, now):
    due(db, 'listings', now, timedelta(hours=ctx.config.sync.listings_refresh_hours))
    return {'listings': refresh_listings(db, ctx.ebay, now)}


def refresh_listings(db, api, now):
    listings = api.listings(now.date().isoformat())
    with db:
        return store_listings(db, listings, now)


def gmail_rate_limited(exc):
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    text = str(exc).lower()
    return status in (403, 429) and ('quota' in text or 'rate' in text)


def step_gmail(db, ctx, now):
    config = ctx.config
    personal = config.matching.personal_recipients
    reparsed = reparse_emails(db, now, personal)  # Stored emails benefit from parser improvements.
    gmail = ctx.gmail
    last = parse_stamp(get_state(db, 'gmail').get('last_success_at'))
    after = (last - timedelta(hours=1)) if last else accounting_start(config)
    stored = 0
    try:
        ids = gmail.search(f'{config.gmail.query} after:{int(after.timestamp())}')
        known = {r[0] for r in db.execute('SELECT gmail_message_id FROM amazon_email_purchases')}
        new = [i for i in ids if i not in known]
        for message_id in new[:GMAIL_MESSAGES_PER_CYCLE]:
            store_email(db, gmail.get(message_id), now, personal)
            stored += 1
            if GMAIL_FETCH_PAUSE_SECONDS:
                time.sleep(GMAIL_FETCH_PAUSE_SECONDS)
    except Exception as exc:
        if gmail_rate_limited(exc):
            raise Skipped(f'Gmail rate limit reached after {stored} new emails; continuing next cycle') from None
        raise
    if len(new) > GMAIL_MESSAGES_PER_CYCLE:
        raise Skipped(f'{len(new) - GMAIL_MESSAGES_PER_CYCLE} emails left; continuing next cycle')
    return {'new_emails': len(new), 'reparsed': reparsed}


def is_personal(parsed, personal):
    return bool(parsed.get('first_name')) and parsed['first_name'].lower() in {p.lower() for p in personal}


def reparse_emails(db, now=None, personal=()):
    """Re-run the parser on stored emails it could not fully read (their redacted bodies are kept
    for exactly this). Emails that now parse drop their body and become matchable; their
    fetched_at moves to `now` so the matcher re-checks older orders against them."""
    rows = db.execute("SELECT gmail_message_id, subject, body FROM amazon_email_purchases "
                      "WHERE parse_status != 'parsed' AND body IS NOT NULL").fetchall()
    improved = 0
    with db:
        for row in rows:
            parsed = parse(row['subject'], row['body'], '<' in row['body'][:500])
            if is_personal(parsed, personal):
                parsed['parse_status'] = 'personal'
            done = parsed['parse_status'] == 'parsed'
            improved += done
            db.execute('UPDATE amazon_email_purchases SET first_name=?, city=?, state=?, quantity=?, category=?, '
                       'grand_total_cents=?, parse_status=?, parse_error=?, body=?, '
                       'fetched_at=CASE WHEN ? THEN ? ELSE fetched_at END WHERE gmail_message_id=?',
                       (parsed['first_name'], parsed['city'], parsed['state'], parsed['quantity'],
                        parsed['category'], parsed['grand_total_cents'], parsed['parse_status'],
                        parsed['parse_error'], None if done else row['body'],
                        int(done and now is not None), stamp(now) if now else None, row['gmail_message_id']))
        # The seller's own purchases are never matched (unless already linked by hand).
        for name in personal:
            db.execute('''UPDATE amazon_email_purchases SET parse_status='personal', body=NULL
                          WHERE parse_status='parsed' AND lower(first_name)=lower(?) AND NOT EXISTS
                          (SELECT 1 FROM order_matches m WHERE m.gmail_message_id=amazon_email_purchases.gmail_message_id)''',
                       (name,))
    return improved


def store_email(db, message, now, personal=()):
    parsed = parse(message['subject'], message['body'], message['is_html'])
    if is_personal(parsed, personal):
        parsed['parse_status'] = 'personal'  # The seller's own purchase: kept, never matched.
    received = datetime.fromtimestamp(message['internal_date_ms'] / 1000, timezone.utc)
    with db:
        db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, first_name, city,
                      state, quantity, category, grand_total_cents, parse_status, parse_error, body, fetched_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(gmail_message_id) DO NOTHING''',
                   (message['id'], stamp(received), message['subject'], parsed['first_name'], parsed['city'],
                    parsed['state'], parsed['quantity'], parsed['category'], parsed['grand_total_cents'],
                    parsed['parse_status'], parsed['parse_error'],
                    # Parsed emails keep only the extracted fields; others keep a redacted body so
                    # the parser can be calibrated on them.
                    None if parsed['parse_status'] in ('parsed', 'personal') else redact(message['body']), stamp(now)))


def step_reconcile(db, ctx, now):
    with db:
        return run_matching(db, ctx.config.matching, now, ctx.config.accounting.start_date)


# Listings last: its daily full download takes minutes and must not delay Gmail and matching.
STEPS = [('orders', step_orders), ('orders_backfill', step_orders_backfill), ('finances', step_finances),
         ('finances_backfill', step_finances_backfill), ('returns', step_returns),
         ('categories', step_categories), ('gmail', step_gmail), ('reconcile', step_reconcile),
         ('listings', step_listings)]


def run_cycle(ctx, now=None, db=None):
    now = now or datetime.now(timezone.utc)
    own = db is None
    db = db or connect(ctx.config.database, migrate=False)
    started = time.monotonic()
    results = {}
    try:
        for name, step in STEPS:
            ctx.current_step = name
            try:
                details = step(db, ctx, now)
                save_state(db, name, now, success=True, details=details)
                results[name] = {'ok': True, **(details or {})}
            except Skipped as skip:
                save_state(db, name, now, details={'skipped': str(skip)})
                results[name] = {'ok': True, 'skipped': str(skip)}
            except Exception as exc:  # One failing step must not stop the others.
                LOG.warning('Sync step %s failed: %s', name, exc)
                save_state(db, name, now, error=str(exc)[:500])
                results[name] = {'ok': False, 'error': str(exc)[:500]}
        ctx.current_step = None
        results['duration_seconds'] = round(time.monotonic() - started, 1)
        save_state(db, 'cycle', now, success=True, details=results)
        return results
    finally:
        ctx.current_step = None
        if own:
            db.close()
