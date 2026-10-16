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
    """Read saved scheduling, progress and error state for one synchronization step.

    Args:
        db: Open SQLite connection used for the query or write.
        name: Sync-step identifier used as the sync_state primary key.

    Returns:
        State dictionary, or an empty dictionary when the step has never run.
    """
    row = db.execute('SELECT * FROM sync_state WHERE name=?', (name,)).fetchone()
    return dict(row) if row else {}


def save_state(db, name, now, *, success=None, error=None, cursor=None, details=None):
    """Merge and commit a step's attempt time, success/error status and optional cursor/details.

    Args:
        db: Open SQLite connection used for the query or write.
        name: Sync-step identifier whose attempt/progress state is updated.
        now: Reference aware datetime used for timing and persisted audit state.
        success: Optional success flag; a truthy value advances last_success_at and clears prior errors.
        error: Optional sanitized error message to record.
        cursor: Optional persisted progress marker; None leaves the previous cursor unchanged.
        details: Optional JSON-serializable step result details to persist.

    Returns:
        None; omitted cursor/details retain their previous values.
    """
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
    """Parse an optional ISO synchronization timestamp.

    Args:
        value: Optional ISO timestamp string; missing/empty values return None.

    Returns:
        Datetime with supplied offset, or None for a missing timestamp.
    """
    return datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def accounting_start(config):
    """Interpret the configured accounting start date as UTC midnight.

    Args:
        config: Project configuration supplying database, integrations and business settings.

    Returns:
        Aware UTC datetime for the start of accounting history.
    """
    return datetime.fromisoformat(config.accounting.start_date).replace(tzinfo=timezone.utc)


class SyncContext:
    """Long-lived clients for the loop (keeps the eBay OAuth token between cycles)."""

    def __init__(self, config, ebay=None, gmail=None):
        """Initialize reusable synchronization clients and mutable cycle progress state.

        Args:
            config: Project configuration supplying database, integrations and business settings.
            ebay: Optional preconstructed eBay client to reuse instead of creating one lazily.
            gmail: Optional preconstructed Gmail client to reuse instead of creating one lazily.

        Returns:
            None; stores configuration and optional injected clients without creating new clients yet.
        """
        self.config = config
        self._ebay = ebay
        self._gmail = gmail
        self.force = False
        self.notifications = None
        self.current_step = None  # Shown in the UI while a cycle runs.

    @property
    def ebay(self):
        """Lazily construct and cache the eBay integration client used across synchronization cycles.

        Args:
            None.

        Returns:
            Cached EbayClient configured for this context.
        """
        if self._ebay is None:
            self._ebay = EbayClient(self.config.ebay)
        return self._ebay

    @property
    def gmail(self):
        """Lazily construct and cache Gmail access, treating disabled or missing setup as a skipped step.

        Args:
            None.

        Returns:
            Cached GmailClient; raises Skipped when Gmail cannot be configured.
        """
        if self._gmail is None:
            if not self.config.gmail.enabled:
                raise Skipped('Gmail disabled in config')
            try:
                self._gmail = GmailClient(self.config.gmail.token_path)
            except GmailNotConfigured as exc:
                raise Skipped(str(exc)) from None
        return self._gmail


def backfill(db, name, start, now, chunk_days, fetch, store):
    """Advance resumable historical ingestion by a bounded number of committed chunks.

    Advance a resumable creation/transaction-date backfill by a few chunks. Once it
    reaches the present it is marked complete; incremental sync takes over.

    Args:
        db: Open SQLite connection used for the query or write.
        name: Backfill sync-state identifier storing the resumable cursor.
        start: Aware initial history datetime used when there is no saved cursor.
        now: Reference aware datetime used for timing and persisted audit state.
        chunk_days: Maximum number of days fetched in each historical ingestion chunk.
        fetch: Callable accepting start/end timestamp strings and returning rows for a history chunk.
        store: Callable that persists fetched history rows using the enclosing connection.

    Returns:
        Dictionary of backfilled row count and completion state; completed cursors perform no fetches.
    """
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
    """Fetch recently modified orders with overlap and commit their ingestion.

    Live sync: orders created or changed since the last success (with overlap).

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary containing the number of changed orders fetched.
    """
    since = parse_stamp(get_state(db, 'orders').get('last_success_at'))
    since = (since - timedelta(minutes=ctx.config.sync.order_overlap_minutes)) if since else now - timedelta(days=2)
    changed = ctx.ebay.raw_orders(stamp(since), stamp(now), 'lastmodifieddate')
    with db:
        store_orders(db, changed, now)
    return {'changed': len(changed)}


def step_orders_backfill(db, ctx, now):
    """Resume bounded order-history chunks from the configured accounting start date.

    History since accounting.start_date, a few chunks per cycle. A separate step so a
    failing chunk never blocks live order sync. Sales it adds are tagged 'ebay_backfill'.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Backfill count/completion dictionary; imported sales use the ebay_backfill source.
    """
    config = ctx.config
    return backfill(db, 'orders_backfill', accounting_start(config), now, config.sync.backfill_chunk_days,
                    lambda a, b: ctx.ebay.raw_orders(a, b),
                    lambda rows: store_orders(db, rows, now, source='ebay_backfill'))


def require_finances_scope(ctx):
    """Refresh eBay access and verify consent permits authoritative Finances reads.

    Args:
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.

    Returns:
        None; raises RuntimeError with setup guidance when the scope is missing.
    """
    ctx.ebay.access_token()
    if ctx.ebay.finances_scope_missing:
        raise RuntimeError('eBay consent lacks the Finances scope; run python ebay_auth.py again. '
                           'Revenue is shown as a fee estimate until then.')


def step_finances(db, ctx, now):
    """Fetch recent posted transactions with a two-day overlap and commit refreshed finances.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary containing transaction count and number of order records updated.
    """
    require_finances_scope(ctx)
    since = parse_stamp(get_state(db, 'finances').get('last_success_at'))
    since = (since - timedelta(days=2)) if since else now - timedelta(days=2)  # Late postings.
    rows = ctx.ebay.finance_transactions(stamp(since), stamp(now))
    with db:
        updated = store_transactions(db, rows, now)
    return {'transactions': len(rows), 'orders_updated': updated}


def step_finances_backfill(db, ctx, now):
    """Resume bounded transaction-history chunks after checking Finances consent.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Backfill count/completion dictionary.
    """
    require_finances_scope(ctx)
    config = ctx.config
    return backfill(db, 'finances_backfill', accounting_start(config), now, config.sync.backfill_chunk_days,
                    ctx.ebay.finance_transactions, lambda rows: store_transactions(db, rows, now))


def due(db, name, now, interval):
    """Check whether an interval-limited sync step is ready based on its last success.

    Args:
        db: Open SQLite connection used for the query or write.
        name: Interval-limited sync-step identifier to inspect.
        now: Reference aware datetime used for timing and persisted audit state.
        interval: Minimum timedelta required between successful scheduled step runs.

    Returns:
        None when due; raises Skipped when the interval has not elapsed.
    """
    last = parse_stamp(get_state(db, name).get('last_success_at'))
    if last and now - last < interval:
        raise Skipped('not due')


def step_returns(db, ctx, now):
    """Fetch accounting-period return cases and commit their effective status inputs.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary containing applied return count; honors the interval unless manually forced.
    """
    if not ctx.force:
        due(db, 'returns', now, timedelta(minutes=ctx.config.sync.returns_interval_minutes))
    members = ctx.ebay.returns(stamp(accounting_start(ctx.config)), stamp(now))
    with db:
        return {'returns': store_returns(db, members, now)}


def step_categories(db, ctx, now):
    """Fill missing category/image metadata for sold items through the shared cache writer.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary containing successful lookup count.
    """
    return {'looked_up': store_categories(db, ctx.ebay)}


def step_listings(db, ctx, now):
    """Refresh eBay listings when due or when explicitly forced.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary containing listing count; raises Skipped when its scheduled interval has not elapsed.
    """
    if not ctx.force:
        due(db, 'listings', now, timedelta(hours=ctx.config.sync.listings_refresh_hours))
    return {'listings': refresh_listings(db, ctx.ebay, now)}


def refresh_listings(db, api, now):
    """Download current active/unsold listings and commit shared listing ingestion.

    Args:
        db: Open SQLite connection used for the query or write.
        api: eBay client used to fetch listing or item metadata.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Number of listings stored.
    """
    listings = api.listings(now.date().isoformat())
    with db:
        return store_listings(db, listings, now)


def gmail_rate_limited(exc):
    """Recognize Gmail quota/rate exceptions from their response status and message.

    Args:
        exc: Exception to inspect for Gmail quota/rate-limit signals.

    Returns:
        True for quota/rate-related HTTP 403 or 429 errors, otherwise False.
    """
    status = getattr(getattr(exc, 'resp', None), 'status', None)
    text = str(exc).lower()
    return status in (403, 429) and ('quota' in text or 'rate' in text)


def step_gmail(db, ctx, now):
    """Reparse stored confirmations and ingest a paced batch of newly discovered Gmail messages.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary with new_emails and reparsed counts; raises Skipped for quota limits or remaining batches.
    """
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
    """Identify parsed confirmations addressed to a configured personal recipient.

    Args:
        parsed: Parsed Amazon confirmation fields including first_name and parse status.
        personal: Recipient first names identifying purchases that must not be auto-matched.

    Returns:
        True when the parsed first name case-insensitively matches the personal-recipient list.
    """
    return bool(parsed.get('first_name')) and parsed['first_name'].lower() in {p.lower() for p in personal}


def reparse_emails(db, now=None, personal=()):
    """Reparse retained redacted bodies and exclude personal purchases, committing all parser improvements.

    Re-run the parser on stored emails it could not fully read (their redacted bodies are kept
    for exactly this). Emails that now parse drop their body and become matchable; their
    fetched_at moves to `now` so the matcher re-checks older orders against them.

    Args:
        db: Open SQLite connection used for the query or write.
        now: Optional timestamp to assign to newly matchable emails; None preserves their previous fetched_at.
        personal: Recipient first names identifying excluded personal purchases; defaults to an empty tuple.

    Returns:
        Number of emails newly parsed successfully; successful bodies are dropped and optional fetch timestamps updated.
    """
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
    """Parse and commit one Gmail message, retaining only necessary fields or a redacted unparsed body.

    Args:
        db: Open SQLite connection used for the query or write.
        message: Gmail message dictionary with id, subject, body, is_html and internal_date_ms.
        now: Reference aware datetime used for timing and persisted audit state.
        personal: Recipient first names identifying excluded personal purchases; defaults to an empty tuple.

    Returns:
        None; duplicate message IDs are ignored and personal purchases remain unmatchable.
    """
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
    """Run and commit automatic order/email reconciliation for the accounting period.

    Args:
        db: Open SQLite connection used for the query or write.
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Reference aware datetime used for timing and persisted audit state.

    Returns:
        Dictionary of counts by resulting match status.
    """
    with db:
        return run_matching(db, ctx.config.matching, now, ctx.config.accounting.start_date)


# Listings last: its daily full download takes minutes and must not delay Gmail and matching.
STEPS = [('orders', step_orders), ('orders_backfill', step_orders_backfill), ('finances', step_finances),
         ('finances_backfill', step_finances_backfill), ('returns', step_returns),
         ('categories', step_categories), ('gmail', step_gmail), ('reconcile', step_reconcile),
         ('listings', step_listings)]


def run_cycle(ctx, now=None, db=None, step_name=None):
    """Execute all sync steps or one selected step, isolate failures, persist progress and wake sale notifications.

    Args:
        ctx: Shared SyncContext containing configuration, reusable clients and force/progress state.
        now: Optional common attempt timestamp; None uses current UTC time.
        db: Optional SQLite connection; None opens and later closes a connection from configuration.
        step_name: Optional sync-step identifier; None runs the complete cycle.

    Returns:
        Step-result dictionary plus duration_seconds; closes only a connection it created.
    """
    if step_name is not None and step_name not in dict(STEPS):
        raise ValueError('Unknown sync step')
    now = now or datetime.now(timezone.utc)
    own = db is None
    db = db or connect(ctx.config.database, migrate=False)
    started = time.monotonic()
    results = {}
    try:
        ctx.force = step_name is not None
        for name, step in STEPS:
            if step_name is not None and name != step_name:
                continue
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
        if step_name is None:
            save_state(db, 'cycle', now, success=True, details=results)
        if ctx.notifications is not None:
            ctx.notifications.trigger()
        return results
    finally:
        ctx.force = False
        ctx.current_step = None
        if own:
            db.close()
