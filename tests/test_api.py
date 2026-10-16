import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db.connection import connect
from backend.services.sync.ebay_sync import store_orders
from factories import amazon_email, ebay_order


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Construct an eBay client using this callable fake as its HTTP transport.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Yields:
        EbayClient instance backed by an HTTPX MockTransport.
    """
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    config['accounting'] = dict(config['accounting'], ordering_fee_cents=0)  # Fee rules: test_ordering_fee.py.
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    db = connect(config['database'])
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    with db:
        store_orders(db, [ebay_order('A', created=created), ebay_order('B', created=created, name='Bob')],
                     datetime.now(timezone.utc))
        amazon_email(db, 'm1', created, total_cents=2000)
    db.close()
    with TestClient(app) as test_client:
        yield test_client


def test_overview_orders_and_detail(client):
    """Regression check: overview orders and detail.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    overview = client.get('/api/overview?range=1w').json()
    assert overview['cards']['total_orders'] == 2 and overview['cards']['awaiting_cost'] == 2
    assert len(overview['series']) == 7
    assert client.get('/api/overview?range=5y').status_code == 400
    listing = client.get('/api/orders?range=30d').json()
    assert listing['total'] == 2 and 'raw_json' not in listing['orders'][0]
    detail = client.get('/api/orders/A').json()
    assert detail['ebay_order_url'].endswith('orderid=A')
    assert detail['items'][0]['ebay_item_url'] == 'https://www.ebay.com/itm/111'
    assert detail['ship_name'] == 'Jane Doe' and 'amazon_order_id' not in json.dumps(detail).lower()
    assert client.get('/api/orders/missing').status_code == 404


def test_manual_actions_through_the_api(client):
    """Regression check: manual actions through the API.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert client.post('/api/orders/A/amazon-cost', json={'amount': 'abc'}).status_code == 400
    order = client.post('/api/orders/A/amazon-cost', json={'amount': '$21.50'}).json()
    assert (order['amazon_cost_cents'], order['match_status']) == (2150, 'MANUAL')
    assert client.post('/api/orders/B/match', json={'gmail_message_id': 'm1'}).json()['amazon_cost_cents'] == 2000
    assert client.post('/api/orders/A/match', json={'gmail_message_id': 'm1'}).status_code == 409
    assert client.delete('/api/orders/B/match').json()['match_status'] == 'PENDING'
    assert client.put('/api/orders/A/return-status', json={'status': 'RETURN_COMPLETED'}).json()['effective_profit_cents'] == 0
    assert client.put('/api/orders/A/return-status', json={'status': 'BOGUS'}).status_code == 400
    assert [e['gmail_message_id'] for e in client.get('/api/orders/A/emails').json()] == ['m1']


def test_csv_export_filtered_and_all(client):
    """Regression check: CSV export filtered and all.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    response = client.get('/api/orders/export?scope=all')
    assert response.headers['content-type'].startswith('text/csv')
    assert 'attachment; filename="orders-all.csv"' == response.headers['content-disposition']
    lines = response.text.strip().splitlines()
    assert lines[0].startswith('sale_date,product_title') and len(lines) == 3
    filtered = client.get('/api/orders/export?scope=filtered&range=30d&q=bob').text.strip().splitlines()
    assert len(filtered) == 2


def test_weekly_run_requires_confirmation_and_key(client):
    """Regression check: weekly run requires confirmation and key.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert client.post('/api/sourcing/weekly/run', json={}).status_code == 400
    response = client.post('/api/sourcing/weekly/run', json={'confirm': True})
    assert response.status_code == 400 and 'OPENAI_API_KEY' in response.json()['detail']


def test_sync_status_and_sync_now(client):
    """Regression check: sync status and sync now.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    status = client.get('/api/sync/status').json()
    assert status['gmail_connected'] is False and status['loop']['interval_seconds'] == 60
    assert client.post('/api/sync/now').status_code == 200


def test_sourcing_endpoints_without_saved_weeks(client):
    """Regression check: sourcing endpoints without saved weeks.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert client.get('/api/sourcing/week').json() == {'week': None, 'results': []}
    assert 'candidates' in client.get('/api/sourcing/candidates').json()
    assert client.get('/api/sourcing/history').json()['entries'] == []


def test_manual_confirmation_and_recheck_wake_notifications_after_commit(client, monkeypatch):
    """Regression check: manual confirmation and recheck wake notifications after commit.

    Args:
        client: FastAPI TestClient fixture backed by an isolated database with background
            syncing disabled.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    app = client.app
    observed = []

    def wake():
        """Read matches through a separate SQLite connection when notifications are triggered, proving API changes are committed.

        Args:
            None.

        Returns:
            None. Appends the visible (order_id, status, manual_cost_cents) rows to the captured
            observations.
        """
        db = connect(app.state.config.database)
        try:
            # A separate connection must see the committed changes before delivery is scheduled.
            observed.append([tuple(r) for r in db.execute(
                'SELECT order_id, status, manual_cost_cents FROM order_matches ORDER BY order_id')])
        finally:
            db.close()

    monkeypatch.setattr(app.state.notifications, 'trigger', wake)
    assert client.post('/api/orders/A/amazon-cost', json={'amount': '21.50'}).status_code == 200
    assert observed[-1] == [('A', 'MANUAL', 2150)]
    assert client.post('/api/orders/B/match', json={'gmail_message_id': 'm1'}).status_code == 200
    assert observed[-1] == [('A', 'MANUAL', 2150), ('B', 'MANUAL', None)]
    assert client.post('/api/orders/B/recheck').status_code == 200
    assert len(observed) == 3
    assert app.state.loop.task is None
