"""Exercise real app lifecycle, HTTP actions, sync workers and mocked Discord together."""
import json
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db.connection import connect
from backend.services.sync.ebay_sync import stamp, store_orders, store_transactions
from factories import amazon_email, ebay_order, sale_transaction

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize('action', ['manual_cost', 'manual_link', 'individual_matching', 'order_recheck'])
def test_confirmation_delivers_via_app_worker_without_background_sync(tmp_path, monkeypatch, action):
    """Regression check: confirmation delivers via app worker without background sync.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        action: Confirmation path exercised through the API: manual cost, manual link, matching
            sync, or recheck.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    monkeypatch.setenv('DISCORD_SALES_WEBHOOK_URL', 'https://discord.com/api/webhooks/123/synthetic-token')
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'out'))
    config['sync']['enabled'] = False
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps(config))
    app = create_app(str(config_path), env_path=str(tmp_path / 'none.env'))
    db = connect(config['database'])
    with db:
        db.execute("UPDATE notification_settings SET activated_at=? WHERE name='discord_sales'",
                   (stamp(NOW - timedelta(days=1)),))
        store_orders(db, [ebay_order('PRIVATE-ORDER', created=stamp(NOW))], NOW)
        store_transactions(db, [sale_transaction('PRIVATE-ORDER', 40)], NOW)
        amazon_email(db, 'synthetic-email', stamp(NOW + timedelta(minutes=5)), total_cents=3000)
    sent, checked = threading.Event(), threading.Event()
    outgoing = []

    def discord(request):
        """Capture the outgoing synthetic Discord payload and signal that the real app worker reached its mocked transport.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX 200 Response containing a synthetic confirmed message identifier.
        """
        outgoing.append(json.loads(request.content))
        sent.set()
        return httpx.Response(200, json={'id': 'synthetic-message'})

    http = httpx.Client(transport=httpx.MockTransport(discord))
    process = app.state.notifications.process

    def deliver():
        """Run notification processing against the captured mock HTTP client and fixed clock, then signal scan completion.

        Args:
            None.

        Returns:
            None. Updates captured events and the notifier's durable delivery records.
        """
        process(now=NOW, client=http)
        checked.set()

    monkeypatch.setattr(app.state.notifications, 'process', deliver)
    try:
        with TestClient(app) as client:
            assert checked.wait(3)  # Initial scan cannot notify an unmatched order.
            assert not sent.is_set()
            assert app.state.loop.task is None
            if action == 'manual_cost':
                response = client.post('/api/orders/PRIVATE-ORDER/amazon-cost', json={'amount': '30.00'})
            elif action == 'manual_link':
                response = client.post('/api/orders/PRIVATE-ORDER/match', json={'gmail_message_id': 'synthetic-email'})
            elif action == 'individual_matching':
                response = client.post('/api/sync/steps/reconcile')
            else:
                response = client.post('/api/orders/PRIVATE-ORDER/recheck')
            assert response.status_code == 200
            assert sent.wait(3), 'Committed confirmation did not wake the notification worker'
        # Shutdown drains delivery, including its durable database write.
        assert len(outgoing) == 1
        fields = {field['name']: field['value'] for field in outgoing[0]['embeds'][0]['fields']}
        assert fields['🧮 Profit breakdown'].endswith('= 💰 Final profit: **+$9.70**')
        assert fields['💳 Amazon cashback (5.0%)'] == '+$1.50'
        assert '🔗 Match / cost confirmed' in fields
        assert 'PRIVATE-ORDER' not in json.dumps(outgoing)
        record = db.execute('SELECT sent_at, message_id FROM sale_notifications').fetchone()
        assert record['sent_at'] and record['message_id'] == 'synthetic-message'
        app.state.notifications.process()
        assert len(outgoing) == 1
    finally:
        http.close()
        db.close()
