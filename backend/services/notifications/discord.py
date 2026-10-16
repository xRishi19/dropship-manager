"""Persistent, serialized Discord delivery. Secrets never enter status/errors."""
import asyncio
import logging
import math
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from ...db.connection import connect
from ..financials.ledger import load_orders
from ..sync.ebay_sync import stamp

LOG = logging.getLogger(__name__)


class WebhookTestError(ValueError):
    def __init__(self, message, status_code=502):
        """Carry a sanitized webhook failure message with the HTTP status suitable for API responses.

        Args:
            message: Sanitized error message safe to expose to the frontend.
            status_code: HTTP status used when translating a safe webhook error into an API response.

        Returns:
            None; initializes the error text and status_code attribute.
        """
        super().__init__(message)
        self.status_code = status_code


def rate_limit_delay(response):
    """Read and bound Discord's retry_after duration without trusting malformed response data.

    Args:
        response: HTTP response containing Discord's retry_after JSON value.

    Returns:
        Delay in seconds clamped to 1–86400; invalid values fall back to 60.
    """
    try:
        delay = float(response.json().get('retry_after', 60))
        return max(1, min(delay, 86400)) if math.isfinite(delay) else 60
    except (TypeError, ValueError, AttributeError, OverflowError):
        return 60


def eligible(order):
    """Require confirmed cost, paid status and posted finances while excluding refunds and active/completed returns.

    Args:
        order: Enriched ledger order with match/payment/revenue/return state, effective profit, exclusions and refund amount.

    Returns:
        True when the enriched order can produce a real-profit sale notification.
    """
    return (order['match_status'] in {'MATCHED', 'MANUAL'}
            and order['payment_status'] == 'PAID'
            and order['revenue_source'] == 'finances'
            and order['effective_profit_cents'] is not None
            and not order['excluded']
            and order['return_status'] in {'NONE', 'RETURN_CANCELLED'}
            and not (order.get('refund_cents') or 0))


def payload(order, image=None, cashback_rate=0.0):
    """Build a sale embed with product information, accounting breakdown, cashback and event times.

    Args:
        order: Eligible ledger order with items, revenue, Amazon cost, ordering fee,
            effective profit, creation time and match timestamps; currency is optional.
        image: Optional product-image URL; only HTTPS URLs become thumbnails.
        cashback_rate: Configured cashback fraction applied to the Amazon cost and displayed
            separately from profit, defaulting to zero.

    Returns:
        Discord payload dictionary with disabled mentions and an optional HTTPS thumbnail.
    """
    titles = [f"{i['title']}" + (f" ×{i['quantity']}" if i['quantity'] > 1 else '') for i in order['items']]
    currency = order.get('fin_currency') or order.get('currency') or 'USD'

    def money(cents, signed=False):
        """Format an integer-cent amount in this order's currency without float rounding.

        Args:
            cents: Monetary amount in integer cents.
            signed: Whether to prefix nonnegative values with a plus sign.

        Returns:
            Currency-formatted amount, retaining a minus sign for negative values.
        """
        amount = Decimal(cents) / 100
        sign = '-' if amount < 0 else '+' if signed else ''
        return f'{sign}${abs(amount):,.2f}' if currency == 'USD' else f'{sign}{abs(amount):,.2f} {currency}'

    cost = order['amazon_cost_cents']
    profit = order['effective_profit_cents']
    cashback = round(cost * cashback_rate)
    breakdown = (f"🏦 eBay take-home: **{money(order['net_revenue_cents'])}**\n"
                 f"− 🛒 Amazon cost: **{money(cost)}**\n"
                 f"− 📦 Ordering fee: **{money(order['ordering_fee_cents'])}**\n"
                 f"= 💰 Final profit: **{money(profit, signed=True)}**")
    embed = {'title': '✅ Sale confirmed!', 'description': ('\n'.join(titles) or 'Item sold')[:4096],
             'color': 0x3FB950 if profit >= 0 else 0xE3B341,
             'fields': [{'name': '🧮 Profit breakdown', 'value': breakdown},
                        {'name': f'💳 Amazon cashback ({cashback_rate:.1%})',
                         'value': money(cashback, signed=True)}],
             'footer': {'text': 'Cashback is shown separately and is not included in final profit.'}}
    for label, value in [('🕒 Sale time', order.get('created_at')),
                         ('🔗 Match / cost confirmed', order.get('matched_at') or order.get('match_updated_at'))]:
        if value:
            try:
                moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if moment.tzinfo is not None:
                    embed['fields'].append({'name': label, 'value': f'<t:{int(moment.timestamp())}:f>', 'inline': True})
            except (ValueError, TypeError, OverflowError, AttributeError):
                pass  # Missing/invalid event timestamps must not block financial notifications.
    if image and image.startswith('https://'):
        embed['thumbnail'] = {'url': image}
    return {'embeds': [embed], 'allowed_mentions': {'parse': []}}


class SaleNotifier:
    def __init__(self, config, webhook_url=None):
        """Validate webhook configuration without exposing secrets and persist the first activation cutoff.

        Args:
            config: Project configuration supplying database, integrations and business settings.
            webhook_url: Optional Discord webhook secret; empty or None disables delivery.

        Returns:
            None; initializes locking/worker state and inserts activation state only when configured.
        """
        self.config = config
        self.url = (webhook_url or '').strip()
        self.lock = threading.Lock()
        self.wake = asyncio.Event()
        self.event_loop = None
        self.task = None
        # Validate without echoing a potentially secret URL in an exception.
        self.enabled = bool(self.url)
        try:
            parts = urlsplit(self.url)
            self.valid = (parts.scheme == 'https' and parts.hostname == 'discord.com'
                          and parts.path.startswith('/api/webhooks/') and not parts.username)
        except ValueError:
            self.valid = False
        if self.enabled:
            db = connect(config.database, migrate=False)
            try:
                with db:
                    db.execute("INSERT OR IGNORE INTO notification_settings VALUES ('discord_sales', ?)",
                               (stamp(datetime.now(timezone.utc)),))
            finally:
                db.close()

    def start(self):
        """Start the serialized delivery worker when a webhook has been configured.

        Args:
            None.

        Returns:
            None; records the running event loop and background task when enabled.
        """
        if self.enabled:
            self.event_loop = asyncio.get_running_loop()
            self.task = asyncio.create_task(self._run())

    def trigger(self):
        """Wake the notification worker safely from API threads or synchronization code.

        Args:
            None.

        Returns:
            None; does nothing until a worker event loop is attached.
        """
        if self.event_loop is not None:
            self.event_loop.call_soon_threadsafe(self.wake.set)

    def send_test(self, client=None):
        """Send a labeled synthetic embed under the delivery lock without creating sale-delivery records.

        Send a synthetic message without changing any sale delivery records.

        Args:
            client: Optional injected HTTP client; an internally created client is closed after the request.

        Returns:
            Success/message dictionary; raises sanitized WebhookTestError for configuration, cooldown or delivery failures.
        """
        if not self.enabled:
            raise WebhookTestError('Discord webhook is not configured. Set DISCORD_SALES_WEBHOOK_URL and restart the backend.', 400)
        if not self.valid:
            raise WebhookTestError('Discord webhook configuration is invalid.', 400)
        if not self.lock.acquire(blocking=False):
            raise WebhookTestError('Discord delivery is busy. Try again shortly.', 409)
        own_client = client is None
        try:
            db = connect(self.config.database, migrate=False)
            try:
                retry = db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_retry'").fetchone()
                if retry and retry[0] > stamp(datetime.now(timezone.utc)):
                    raise WebhookTestError('Discord delivery is cooling down. Try again later.', 429)
            finally:
                db.close()
            sample_time = stamp(datetime.now(timezone.utc))
            sample = payload({'items': [{'title': 'Example item (test only)', 'quantity': 1}],
                              'currency': 'USD', 'net_revenue_cents': 4264, 'amazon_cost_cents': 3000,
                              'ordering_fee_cents': self.config.accounting.ordering_fee_cents,
                              'effective_profit_cents': 1264 - self.config.accounting.ordering_fee_cents,
                              'created_at': sample_time, 'matched_at': sample_time},
                             cashback_rate=self.config.accounting.prime_cashback_rate)
            sample['embeds'][0]['title'] = '🧪 Test webhook — connection successful!'
            sample['embeds'][0]['footer'] = {'text': 'Sample data only. No actual sale occurred. Cashback is separate from profit.'}
            try:
                client = client or httpx.Client(timeout=10, follow_redirects=False)
                response = client.post(self.url, params={'wait': 'true'}, json=sample)
                if response.status_code == 429:
                    db = connect(self.config.database, migrate=False)
                    try:
                        with db:
                            db.execute("INSERT OR REPLACE INTO notification_settings VALUES ('discord_retry', ?)",
                                       (stamp(datetime.now(timezone.utc) + timedelta(seconds=rate_limit_delay(response))),))
                    finally:
                        db.close()
                    raise WebhookTestError('Discord rate limited the test. Try again later.', 429)
                if response.status_code != 200:
                    raise WebhookTestError(f'Discord rejected the test (HTTP {response.status_code}).')
                if not response.json().get('id'):
                    raise WebhookTestError('Discord did not confirm the test message.')
            except WebhookTestError:
                raise
            except Exception:
                raise WebhookTestError('Could not confirm the test message. Check the webhook configuration and try again.') from None
            return {'ok': True, 'message': 'Test message sent to Discord.'}
        finally:
            if own_client and client is not None:
                client.close()
            self.lock.release()

    async def stop(self):
        """Cancel polling and allow an in-flight delivery to persist its final outcome.

        Args:
            None.

        Returns:
            None once the notification worker has stopped.
        """
        if self.task:
            # Let an in-flight request finish and persist its outcome before shutdown.
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _run(self):
        """Check sale delivery in a worker thread every fifteen seconds or when explicitly awakened.

        Args:
            None.

        Returns:
            None if cancelled; otherwise continues indefinitely.
        """
        while True:
            self.wake.clear()
            job = asyncio.create_task(asyncio.to_thread(self.process))
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                await job
                raise
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    def process(self, now=None, client=None):
        """Deliver due eligible sales serially, persisting deduplication, confirmation and bounded retry state.

        Args:
            now: Optional common delivery-pass timestamp; None uses current UTC time.
            client: Optional injected HTTP client; an internally created client is closed after the delivery pass.

        Returns:
            None; sends at most twenty successful messages per pass and records sanitized failures.
        """
        if not self.enabled or not self.lock.acquire(blocking=False):
            return
        db = None
        own_client = client is None
        try:
            db = connect(self.config.database, migrate=False)
            now = now or datetime.now(timezone.utc)
            if not self.valid:
                self._status(db, now, error='Discord webhook configuration is invalid')
                return
            retry = db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_retry'").fetchone()
            if retry and retry[0] > stamp(now):
                return
            activated = db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_sales'").fetchone()[0]
            # Select undelivered, due orders only; read current financials again just before delivery.
            ids = [r[0] for r in db.execute('''SELECT o.order_id FROM orders o
                LEFT JOIN sale_notifications n ON n.order_id=o.order_id
                WHERE julianday(o.created_at)>=julianday(?) AND n.sent_at IS NULL
                AND (n.next_attempt_at IS NULL OR n.next_attempt_at<=?)
                ORDER BY o.created_at''', (activated, stamp(now)))]
            client = client or httpx.Client(timeout=10, follow_redirects=False)
            sent = 0
            for order_id in ids:
                order = load_orders(db, order_ids=[order_id],
                                    ordering_fee_cents=self.config.accounting.ordering_fee_cents)[0]
                if not eligible(order):
                    continue
                first = order['items'][0] if order['items'] else {}
                image_row = db.execute('SELECT image_url FROM listings WHERE item_id=?',
                                       (first.get('item_id'),)).fetchone()
                image = image_row[0] if image_row else None
                previous = db.execute('SELECT attempts FROM sale_notifications WHERE order_id=?',
                                      (order_id,)).fetchone()
                attempts = (previous[0] if previous else 0) + 1
                error, message_id, retry_after = None, None, min(3600, 15 * 2 ** min(attempts, 8))
                try:
                    response = client.post(self.url, params={'wait': 'true'},
                                           json=payload(order, image, self.config.accounting.prime_cashback_rate))
                    if response.status_code == 200:
                        message_id = response.json().get('id')
                        if not message_id:
                            error = 'Discord did not confirm the message'
                    elif response.status_code == 429:
                        error = 'Discord rate limited delivery'
                        retry_after = rate_limit_delay(response)
                    else:
                        error = f'Discord HTTP {response.status_code}'
                        if 400 <= response.status_code < 500:
                            retry_after = 3600
                except Exception:
                    # HTTP exception strings can contain the webhook token; never persist/log them.
                    error = 'Discord request failed or confirmation was unreadable'
                with db:
                    db.execute('''INSERT INTO sale_notifications
                        (order_id, attempts, next_attempt_at, sent_at, message_id, last_error) VALUES (?,?,?,?,?,?)
                        ON CONFLICT(order_id) DO UPDATE SET attempts=excluded.attempts,
                        next_attempt_at=excluded.next_attempt_at, sent_at=excluded.sent_at,
                        message_id=excluded.message_id, last_error=excluded.last_error''',
                        (order_id, attempts, stamp(now + timedelta(seconds=retry_after)) if error else None,
                         None if error else stamp(now), message_id, error))
                if error:
                    with db:
                        db.execute("INSERT OR REPLACE INTO notification_settings VALUES ('discord_retry', ?)",
                                   (stamp(now + timedelta(seconds=retry_after)),))
                    self._status(db, now, error=error)
                    return  # Avoid hammering a broken or rate-limited webhook.
                sent += 1
                if sent >= 20:
                    break
            self._status(db, now, sent=sent)
        except Exception:
            LOG.warning('Discord delivery worker failed; retrying on the next check')
        finally:
            if own_client and client is not None:
                client.close()
            if db is not None:
                db.close()
            self.lock.release()

    @staticmethod
    def _status(db, now, error=None, sent=0):
        """Persist sanitized Discord delivery health in the shared sync-status table.

        Args:
            db: Open SQLite connection used for the query or write.
            now: Reference aware datetime used for timing and persisted audit state.
            error: Optional sanitized error message to record.
            sent: Number of successfully confirmed messages in the current delivery pass.

        Returns:
            None; commits sent-count details and success/error state.
        """
        from ..sync.orchestrator import save_state
        save_state(db, 'discord', now, success=not error, error=error, details={'sent': sent})
