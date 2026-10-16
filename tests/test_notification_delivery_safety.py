"""Concurrency, shutdown, and malformed-response regression checks; no live requests."""
import asyncio
import threading
from datetime import datetime, timedelta

import httpx
import pytest

from backend.services.sync.loop import SyncLoop
from test_sale_notifications import NOW, WEBHOOK, delivery, sale, transport


@pytest.mark.parametrize('delay', ['NaN', 'Infinity', 'invalid', None, 10 ** 100])
def test_invalid_rate_limit_delay_still_persists_a_bounded_retry(delivery, delay):
    """Regression check: invalid rate limit delay still persists a bounded retry.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        delay: Untrusted Discord retry_after value being tested for bounded retry persistence.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    client, requests = transport(lambda _: httpx.Response(429, json={'retry_after': delay}))
    try:
        notifier.process(NOW, client)
        row = db.execute('SELECT * FROM sale_notifications').fetchone()
        assert row is not None and row['attempts'] == 1
        assert not row['sent_at']
        assert row['last_error'] == 'Discord rate limited delivery'
        next_attempt = datetime.fromisoformat(row['next_attempt_at'].replace('Z', '+00:00'))
        assert NOW < next_attempt <= NOW + timedelta(days=1)
        notifier.process(NOW + timedelta(seconds=.5), client)
        assert len(requests) == 1
    finally:
        client.close()


@pytest.mark.parametrize('status', [201, 204, 400, 401, 403, 500])
def test_unconfirmed_http_responses_never_persist_response_secrets(delivery, caplog, status):
    """Regression check: unconfirmed http responses never persist response secrets.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        caplog: Pytest log capture fixture used to inspect messages and ensure sensitive values
            are absent.
        status: Unexpected Discord HTTP response code whose unconfirmed delivery must be
            retried.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    client, _ = transport(lambda _: httpx.Response(status, text=WEBHOOK))
    try:
        notifier.process(NOW, client)
        row = db.execute('SELECT * FROM sale_notifications').fetchone()
        assert row['sent_at'] is None and row['message_id'] is None
        assert row['last_error'] == f'Discord HTTP {status}'
        assert WEBHOOK not in caplog.text
        assert 'private-test-token' not in caplog.text
        state = db.execute("SELECT last_error FROM sync_state WHERE name='discord'").fetchone()
        assert state[0] == f'Discord HTTP {status}'
    finally:
        client.close()


def test_worker_internal_exception_logs_only_sanitized_summary(delivery, caplog, monkeypatch):
    """Regression check: worker internal exception logs only sanitized summary.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.
        caplog: Pytest log capture fixture used to inspect messages and ensure sensitive values
            are absent.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    def fail(*args, **kwargs):
        """Raise a synthetic internal error containing the fixture webhook to test that worker logs do not expose secrets.

        Args:
            args: Positional arguments accepted for compatibility with the production callable being
                mocked.
            kwargs: Keyword arguments forwarded to the mocked callable or used to override synthetic
                payload fields.

        Returns:
            Never returns; raises RuntimeError with the captured synthetic webhook URL.
        """
        raise RuntimeError(WEBHOOK)
    monkeypatch.setattr('backend.services.notifications.discord.load_orders', fail)
    client, requests = transport()
    try:
        notifier.process(NOW, client)
        assert not requests
        assert 'Discord delivery worker failed' in caplog.text
        assert 'private-test-token' not in caplog.text
        assert not notifier.lock.locked()
    finally:
        client.close()


def test_concurrent_delivery_is_serialized(delivery):
    """Regression check: concurrent delivery is serialized.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    entered, release = threading.Event(), threading.Event()

    def send(_):
        """Signal the start of a mock Discord send and block until the test releases the request.

        Args:
            _: Unused intercepted HTTPX request; the handler always returns the configured synthetic
                response.

        Returns:
            HTTPX 200 Response containing a synthetic confirmed message identifier.
        """
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={'id': 'confirmed'})

    client, requests = transport(send)
    worker = threading.Thread(target=lambda: notifier.process(NOW, client))
    worker.start()
    try:
        assert entered.wait(3)
        notifier.process(NOW, client)
        assert len(requests) == 1
    finally:
        release.set()
        worker.join(5)
        client.close()
    assert not worker.is_alive()
    assert db.execute('SELECT attempts, message_id FROM sale_notifications').fetchone()[:] == (1, 'confirmed')


def test_notification_shutdown_waits_until_confirmation_is_persisted(delivery):
    """Regression check: notification shutdown waits until confirmation is persisted.

    Args:
        delivery: Tuple of an isolated SQLite connection and SaleNotifier configured with a
            synthetic webhook.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    db, notifier = delivery
    sale(db)
    entered, release = threading.Event(), threading.Event()

    def send(_):
        """Signal the start of a mock Discord send and block until the test releases the request.

        Args:
            _: Unused intercepted HTTPX request; the handler always returns the configured synthetic
                response.

        Returns:
            HTTPX 200 Response containing a synthetic confirmed message identifier.
        """
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={'id': 'confirmed-before-shutdown'})

    client, requests = transport(send)
    process = notifier.process
    notifier.process = lambda: process(NOW, client)

    async def scenario():
        """Start notification delivery, cancel the worker during its blocked send, and verify shutdown waits for durable confirmation.

        Args:
            None.

        Returns:
            None. Completes the asynchronous lifecycle assertions and shuts down the worker.
        """
        notifier.start()
        assert await asyncio.to_thread(entered.wait, 3)
        stopping = asyncio.create_task(notifier.stop())
        try:
            await asyncio.sleep(.02)
            assert not stopping.done()
        finally:
            release.set()
            await stopping

    try:
        asyncio.run(scenario())
        assert len(requests) == 1
        row = db.execute('SELECT sent_at, message_id FROM sale_notifications').fetchone()
        assert row['sent_at'] and row['message_id'] == 'confirmed-before-shutdown'
    finally:
        release.set()
        client.close()


@pytest.mark.parametrize('manual', [False, True])
def test_sync_shutdown_holds_lock_until_worker_finishes(manual):
    """Regression check: sync shutdown holds lock until worker finishes.

    Args:
        manual: Whether to start a manually submitted sync rather than the automatic polling
            loop.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    entered, release = threading.Event(), threading.Event()

    def operation():
        """Block a synthetic sync operation until released so shutdown lock retention can be tested.

        Args:
            None.

        Returns:
            Dictionary with completed set to True after the release event is signaled.
        """
        entered.set()
        assert release.wait(5)
        return {'completed': True}

    async def scenario():
        """Start a manual or automatic sync, request shutdown while its worker is blocked, and verify the execution lock stays held until completion.

        Args:
            None.

        Returns:
            None. Completes the asynchronous lifecycle assertions and shuts down the worker.
        """
        loop = SyncLoop(operation, interval_seconds=3600)
        if manual:
            assert loop.submit()
        else:
            loop.start()
        assert await asyncio.to_thread(entered.wait, 3)
        stopping = asyncio.create_task(loop.stop())
        try:
            await asyncio.sleep(.02)
            assert not stopping.done()
            assert not loop.lock.acquire(blocking=False)
        finally:
            release.set()
            await stopping
        assert loop.last_result == {'completed': True}
        assert not loop.running
        assert loop.lock.acquire(blocking=False)
        loop.lock.release()

    asyncio.run(scenario())
