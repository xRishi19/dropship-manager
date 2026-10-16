"""The explicit test action uses synthetic data and never sends live requests in tests."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.services.notifications.discord import SaleNotifier, WebhookTestError
from backend.services.sync.ebay_sync import stamp
from test_sale_notifications import delivery, transport
from test_api import client


def test_sample_is_confirmed_and_does_not_change_sale_delivery_state(delivery):
    """Regression check: sample is confirmed and does not change sale delivery state.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    activated = db.execute('SELECT * FROM notification_settings').fetchall()
    http, requests = transport()
    try:
        assert notifier.send_test(http) == {'ok': True, 'message': 'Test message sent to Discord.'}
        body = json.loads(requests[0].content)
        assert requests[0].url.params['wait'] == 'true'
        assert 'Test webhook' in body['embeds'][0]['title']
        assert 'No actual sale' in body['embeds'][0]['footer']['text']
        assert body['allowed_mentions'] == {'parse': []}
        assert db.execute('SELECT count(*) FROM sale_notifications').fetchone()[0] == 0
        assert db.execute('SELECT * FROM notification_settings').fetchall() == activated
        assert notifier.lock.acquire(blocking=False)
        notifier.lock.release()
    finally:
        http.close()


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'busy', 'cooldown'])
def test_unavailable_test_does_not_send(delivery, kind):
    """Regression check: unavailable test does not send.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        kind: Synthetic transaction type or webhook-unavailability case selected by the current
            helper/test.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    if kind == 'missing':
        notifier = SaleNotifier(notifier.config)
    elif kind == 'invalid':
        notifier.valid = False
    elif kind == 'busy':
        notifier.lock.acquire()
    else:
        with db:
            db.execute("INSERT INTO notification_settings VALUES ('discord_retry', ?)",
                       (stamp(datetime.now(timezone.utc) + timedelta(hours=1)),))
    http, requests = transport()
    try:
        with pytest.raises(WebhookTestError):
            notifier.send_test(http)
        assert not requests
    finally:
        if kind == 'busy':
            notifier.lock.release()
        http.close()


@pytest.mark.parametrize('status,body', [(429, {}), (404, {}), (500, {}), (200, {})])
def test_discord_rejections_are_sanitized(delivery, status, body):
    """Regression check: discord rejections are sanitized.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        status: Parameterized status variant, or synthetic purchase/listing status used by the
            helper.
        body: Synthetic HTTP, email, or API request payload used to exercise the current test
            case.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    _, notifier = delivery
    http, _ = transport(lambda _: httpx.Response(status, json={**body, 'secret': notifier.url}))
    try:
        with pytest.raises(WebhookTestError) as exc:
            notifier.send_test(http)
        assert notifier.url not in str(exc.value)
        assert notifier.lock.acquire(blocking=False)
        notifier.lock.release()
    finally:
        http.close()


def test_network_errors_do_not_expose_url(delivery):
    """Regression check: network errors do not expose url.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    _, notifier = delivery
    def fail(request):
        """Raise a timeout containing the intercepted URL to verify error sanitization hides webhook credentials.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            Never returns; raises httpx.ReadTimeout with the synthetic request URL.
        """
        raise httpx.ReadTimeout(str(request.url))
    http, _ = transport(fail)
    try:
        with pytest.raises(WebhookTestError) as exc:
            notifier.send_test(http)
        assert notifier.url not in str(exc.value)
    finally:
        http.close()


def test_api_success_and_configuration_failure(client, monkeypatch):
    """Regression check: API success and configuration failure.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert client.post('/api/sync/webhook/test').status_code == 400
    monkeypatch.setattr(client.app.state.notifications, 'send_test',
                        lambda: {'ok': True, 'message': 'Test message sent to Discord.'})
    response = client.post('/api/sync/webhook/test')
    assert response.status_code == 200 and response.json()['ok'] is True


def test_rate_limited_test_persists_shared_delivery_cooldown(delivery):
    """Regression check: rate limited test persists shared delivery cooldown.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    http, requests = transport(lambda _: httpx.Response(429, json={'retry_after': 120}))
    try:
        with pytest.raises(WebhookTestError):
            notifier.send_test(http)
        retry = db.execute("SELECT activated_at FROM notification_settings WHERE name='discord_retry'").fetchone()[0]
        assert retry > stamp(datetime.now(timezone.utc) + timedelta(seconds=110))
        with pytest.raises(WebhookTestError, match='cooling down'):
            notifier.send_test(http)
        assert len(requests) == 1
    finally:
        http.close()
