import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.config import Config
from backend.db.connection import connect
from backend.services.notifications.discord import SaleNotifier
from backend.services.reconciliation.manual import set_manual_cost, unlink
from backend.services.reconciliation.matcher import run_matching
from backend.services.returns.service import set_manual_status
from backend.services.sync.ebay_sync import stamp, store_categories, store_orders, store_transactions
from factories import amazon_email, charge, ebay_order, sale_transaction

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
WEBHOOK = 'https://discord.com/api/webhooks/123/private-test-token'


@pytest.fixture
def delivery(tmp_path):
    """Create a notifier backed by a temporary database and a synthetic webhook with a fixed activation boundary.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.

    Yields:
        Tuple (db, notifier) for isolated delivery tests; the database connection closes during
        teardown.
    """
    config = Config(database=str(tmp_path / 'app.sqlite3'))
    db = connect(config.database)
    notifier = SaleNotifier(config, WEBHOOK)
    with db:
        db.execute("UPDATE notification_settings SET activated_at=? WHERE name='discord_sales'",
                   (stamp(NOW - timedelta(days=1)),))
    yield db, notifier
    db.close()


def sale(db, order_id='sale', *, cost=3000, finances=True, created=None, **kwargs):
    """Commit a synthetic order and optional posted finances and manual Amazon cost for notification eligibility tests.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        order_id: Synthetic eBay or Amazon order identifier used to distinguish test records.
        cost: Amazon purchase cost in cents; None leaves the cost unknown.
        finances: Whether to seed authoritative SALE and advertising-debit transactions.
        created: Optional ISO order creation timestamp; omitted or empty values use the fixed
            NOW timestamp.
        kwargs: Additional keyword overrides forwarded directly to the ebay_order factory.

    Returns:
        None. Updates isolated order, transaction, financial, and match records.
    """
    with db:
        store_orders(db, [ebay_order(order_id, created=created or stamp(NOW), **kwargs)], NOW)
        if finances:
            store_transactions(db, [sale_transaction(order_id, 40), charge(order_id, 2)], NOW)
        if cost is not None:
            set_manual_cost(db, order_id, cost, NOW)


def transport(handler=None):
    """Create a MockTransport-backed HTTPX client that records webhook requests and can customize responses.

    Args:
        handler: Optional MockTransport callback; None returns a confirmed synthetic Discord
            message.

    Returns:
        Tuple of the mock HTTPX client and the mutable list of captured requests; the caller
        closes the client.
    """
    requests = []

    def send(request):
        """Capture a Discord request and call the optional custom handler, otherwise return a successful confirmation.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX Response from the configured handler or a 200 response with a synthetic message
            identifier.
        """
        requests.append(request)
        return handler(request) if handler else httpx.Response(200, json={'id': 'message'})

    return httpx.Client(transport=httpx.MockTransport(send)), requests


def test_waits_for_matching_and_posted_finances_then_sends_private_profit(delivery):
    """Regression check: waits for matching and posted finances then sends private profit.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db, cost=None, finances=False, items=[('111', 'Product A', 2, ''), ('222', 'Product B', 1, '')])
    client, requests = transport()
    notifier.process(NOW, client)
    assert not requests
    with db:
        set_manual_cost(db, 'sale', 3000, NOW)
    notifier.process(NOW, client)
    assert not requests  # Fulfillment estimates are insufficient.
    with db:
        store_transactions(db, [sale_transaction('sale', 40), charge('sale', 2)], NOW)
        db.execute("UPDATE listings SET image_url='https://i.ebayimg.com/product.jpg' WHERE item_id='111'")
    notifier.process(NOW, client)
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert request.url.params['wait'] == 'true'
    assert body['allowed_mentions'] == {'parse': []}
    embed = body['embeds'][0]
    assert embed['description'] == 'Product A ×2\nProduct B'
    fields = {field['name']: field['value'] for field in embed['fields']}
    assert fields['🧮 Profit breakdown'] == ('🏦 eBay take-home: **$38.00**\n'
        '− 🛒 Amazon cost: **$30.00**\n− 📦 Ordering fee: **$0.30**\n= 💰 Final profit: **+$7.70**')
    assert fields['💳 Amazon cashback (5.0%)'] == '+$1.50'
    assert fields['🕒 Sale time'] == f'<t:{int(NOW.timestamp())}:f>'
    assert fields['🔗 Match / cost confirmed'] == f'<t:{int(NOW.timestamp())}:f>'
    assert embed['thumbnail']['url'].endswith('product.jpg')
    text = request.content.decode()
    for private in ['buyer_', 'Jane', 'Springfield', 'Main St', 'order_id', 'gmail', 'private-test-token']:
        assert private not in text
    client.close()


@pytest.mark.parametrize('changes', [
    {'cost': None}, {'payment': 'PENDING'}, {'payment': 'FULLY_REFUNDED'},
    {'cancelled': True}, {'finances': False}, {'created': '2026-01-01T00:00:00Z'},
])
def test_ineligible_sales_are_not_sent(delivery, changes):
    """Regression check: ineligible sales are not sent.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        changes: Factory keyword overrides that make the synthetic sale ineligible for
            notification.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db, **changes)
    client, requests = transport()
    notifier.process(NOW, client)
    assert not requests
    client.close()


@pytest.mark.parametrize('return_status', ['RETURN_STARTED', 'RETURN_COMPLETED', 'REFUNDED'])
def test_returns_are_excluded(delivery, return_status):
    """Regression check: returns are excluded.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        return_status: Return-status variant applied to the order before checking notification
            eligibility.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    with db:
        set_manual_status(db, 'sale', return_status, NOW)
    client, requests = transport()
    notifier.process(NOW, client)
    assert not requests
    client.close()


def test_partial_refunds_are_excluded(delivery):
    """Regression check: partial refunds are excluded.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    with db:
        store_transactions(db, [charge('sale', 1, kind='REFUND')], NOW)
    client, requests = transport()
    notifier.process(NOW, client)
    assert not requests
    client.close()


@pytest.mark.parametrize(('cost', 'expected'), [(3770, '+$0.00'), (4000, '-$2.30')])
def test_zero_and_negative_profit_and_missing_image(delivery, cost, expected):
    """Regression check: zero and negative profit and missing image.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        cost: Amazon purchase cost in cents; None leaves the cost unknown.
        expected: Expected result for this parametrized input, such as parser fields or a
            formatted profit.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db, cost=cost)
    client, requests = transport()
    notifier.process(NOW, client)
    embed = json.loads(requests[0].content)['embeds'][0]
    assert embed['fields'][0]['value'].endswith(f'= 💰 Final profit: **{expected}**')
    assert 'thumbnail' not in embed
    client.close()


def test_automatic_match_and_restart_deduplication(delivery):
    """Regression check: automatic match and restart deduplication.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db, cost=None)
    with db:
        amazon_email(db, 'email', stamp(NOW + timedelta(minutes=10)))
        run_matching(db, notifier.config.matching, NOW)
    assert db.execute('SELECT status FROM order_matches').fetchone()[0] == 'MATCHED'
    client, requests = transport()
    notifier.process(NOW, client)
    assert len(requests) == 1
    with db:
        unlink(db, 'sale', NOW)
        set_manual_cost(db, 'sale', 2500, NOW)
    restarted = SaleNotifier(notifier.config, WEBHOOK)
    restarted.process(NOW, client)
    assert len(requests) == 1
    assert db.execute('SELECT sent_at FROM sale_notifications').fetchone()[0]
    client.close()


@pytest.mark.parametrize('failure', ['rate', 'server', 'network', 'bad_confirmation'])
def test_retry_is_persistent_sanitized_and_rechecks_eligibility(delivery, failure):
    """Regression check: retry is persistent sanitized and rechecks eligibility.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        failure: Failure mode selecting a mocked network error, rate limit, server error, or
            missing confirmation.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    sale(db, 'second')

    def fail(request):
        """Simulate the captured Discord failure mode with a timeout, rate limit, server error, or missing confirmation.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            Synthetic failing HTTPX response; network-error mode raises ReadTimeout instead.
        """
        if failure == 'network':
            raise httpx.ReadTimeout(str(request.url))
        if failure == 'bad_confirmation':
            return httpx.Response(200, json={})
        return httpx.Response(429 if failure == 'rate' else 503, json={'retry_after': 120})

    client, requests = transport(fail)
    notifier.process(NOW, client)
    assert len(requests) == 1
    row = db.execute('SELECT * FROM sale_notifications').fetchone()
    assert not row['sent_at'] and row['next_attempt_at'] > stamp(NOW)
    assert 'private-test-token' not in row['last_error']
    restarted = SaleNotifier(notifier.config, WEBHOOK)
    restarted.process(NOW + timedelta(seconds=1), client)
    assert len(requests) == 1  # Persisted global cooldown protects other pending sales too.
    with db:
        db.execute('UPDATE orders SET is_cancelled=1 WHERE order_id=?', (row['order_id'],))
    success, sent = transport()
    restarted.process(NOW + timedelta(hours=2), success)
    assert len(sent) == 1  # Previously failed order is now cancelled, so only the other sale sends.
    assert db.execute('SELECT count(*) FROM sale_notifications WHERE sent_at IS NOT NULL').fetchone()[0] == 1
    client.close()
    success.close()


def test_successful_retry_and_activation_survive_restart(delivery):
    """Regression check: successful retry and activation survive restart.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    activated = db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_sales'").fetchone()[0]
    sale(db)
    failed, _ = transport(lambda _: httpx.Response(503))
    notifier.process(NOW, failed)
    restarted = SaleNotifier(notifier.config, WEBHOOK)
    success, requests = transport()
    restarted.process(NOW + timedelta(hours=1), success)
    assert len(requests) == 1
    assert db.execute('SELECT attempts FROM sale_notifications').fetchone()[0] == 2
    assert db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_sales'").fetchone()[0] == activated
    failed.close()
    success.close()


def test_no_starvation_from_unmatched_orders(delivery):
    """Regression check: no starvation from unmatched orders.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    for i in range(105):
        sale(db, str(i), cost=None)
    sale(db, 'ready', created=stamp(NOW + timedelta(seconds=1)))
    client, requests = transport()
    notifier.process(NOW + timedelta(seconds=2), client)
    assert len(requests) == 1
    client.close()


def test_existing_category_lookup_also_caches_image(delivery):
    """Regression check: existing category lookup also caches image.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, _ = delivery
    sale(db)
    with db:
        db.execute("UPDATE listings SET category='Cached category'")
    class Ebay:
        def item_metadata(self, item_id):
            """Provide a fixed category and primary image URL from the synthetic eBay metadata client.

            Args:
                item_id: Synthetic eBay listing identifier whose category or image metadata is
                    requested.

            Returns:
                Dictionary with category and image_url used to verify listing metadata caching.
            """
            return {'category': 'Parts', 'image_url': 'https://i.ebayimg.com/item.jpg'}
    assert store_categories(db, Ebay()) == 1
    row = db.execute('SELECT category, image_url FROM listings').fetchone()
    assert tuple(row) == ('Parts', 'https://i.ebayimg.com/item.jpg')
    assert store_categories(db, Ebay()) == 0


def test_worker_runs_and_wakes_with_background_sync_disabled(delivery):
    """Regression check: worker runs and wakes with background sync disabled.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    seen = []
    notifier.process = lambda: seen.append(True)

    async def scenario():
        """Start the notification worker, wait for its initial scan, trigger another scan, and stop it after both have completed.

        Args:
            None.

        Returns:
            None. Completes the asynchronous lifecycle assertions and shuts down the worker.
        """
        notifier.start()
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(.01)
        notifier.trigger()
        for _ in range(100):
            if len(seen) == 2:
                break
            await asyncio.sleep(.01)
        await notifier.stop()
    asyncio.run(scenario())
    assert len(seen) == 2


def test_invalid_webhook_reports_sanitized_configuration_error(delivery):
    """Regression check: invalid webhook reports sanitized configuration error.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    invalid = SaleNotifier(notifier.config, 'https://[invalid-secret')
    client, requests = transport()
    invalid.process(NOW, client)
    assert not requests
    error = db.execute("SELECT last_error FROM sync_state WHERE name='discord'").fetchone()[0]
    assert error == 'Discord webhook configuration is invalid'
    client.close()


def test_notification_migration_preserves_confirmed_orders(tmp_path, monkeypatch):
    """Regression check: notification migration preserves confirmed orders.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    from backend.db import connection
    path = str(tmp_path / 'upgrade.sqlite3')
    current = connection.MIGRATIONS
    monkeypatch.setattr(connection, 'MIGRATIONS', current[:3])
    old = connect(path)
    sale(old)
    old.close()
    monkeypatch.setattr(connection, 'MIGRATIONS', current)
    upgraded = connect(path)
    assert upgraded.execute('SELECT status FROM order_matches').fetchone()[0] == 'MANUAL'
    assert upgraded.execute('SELECT count(*) FROM sale_notifications').fetchone()[0] == 0
    assert upgraded.execute('SELECT image_url, image_checked FROM listings').fetchone()[:] == (None, 0)
    upgraded.close()
    assert len(list((tmp_path / 'backups').iterdir())) == 1


@pytest.mark.parametrize(('rate', 'expected'), [(0, '+$0.00'), (0.05, '+$1.73'), (0.075, '+$2.59')])
def test_cashback_uses_configured_rate_and_stays_separate_from_profit(delivery, rate, expected):
    """Show rounded per-order cashback at the configured rate without adding it to profit.

    Args:
        delivery: Isolated database and notifier fixture configured with a synthetic webhook.
        rate: Cashback fraction supplied through the accounting configuration.
        expected: Expected rounded and signed cashback amount in USD.

    Returns:
        None. Asserts the posted breakdown and cashback independently using mocked Discord.
    """
    db, notifier = delivery
    notifier.config = notifier.config.model_copy(update={
        'accounting': notifier.config.accounting.model_copy(update={'prime_cashback_rate': rate})})
    sale(db, cost=3455)
    http, requests = transport()
    try:
        notifier.process(NOW, http)
        fields = {field['name']: field['value'] for field in json.loads(requests[0].content)['embeds'][0]['fields']}
        assert fields[f'💳 Amazon cashback ({rate:.1%})'] == expected
        assert fields['🧮 Profit breakdown'].endswith('= 💰 Final profit: **+$3.15**')
    finally:
        http.close()


def test_payload_currency_and_missing_timestamps():
    """Format the expanded breakdown in a non-USD currency and omit unusable event times.

    Args:
        None.

    Returns:
        None. Verifies currency labels, negative profit, and resilience to missing/invalid times.
    """
    from backend.services.notifications.discord import payload
    embed = payload({'items': [{'title': 'Example', 'quantity': 1}], 'currency': 'EUR',
                     'net_revenue_cents': 3000, 'amazon_cost_cents': 3500,
                     'ordering_fee_cents': 30, 'effective_profit_cents': -530,
                     'created_at': 'invalid', 'matched_at': None})['embeds'][0]
    assert 'eBay take-home: **30.00 EUR**' in embed['fields'][0]['value']
    assert embed['fields'][0]['value'].endswith('= 💰 Final profit: **-5.30 EUR**')
    assert len(embed['fields']) == 2
    assert embed['color'] == 0xE3B341
