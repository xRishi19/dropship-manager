import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db.connection import connect
from backend.services.financials.aggregation import series, time_range, utc_iso
from backend.services.sync.ebay_sync import store_orders
from factories import ebay_order

NOW = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)


def test_february_is_28_daily_buckets_between_local_midnights():
    zone = ZoneInfo('America/Los_Angeles')
    start, end, granularity, buckets = time_range('2026-02', NOW, zone)
    assert granularity == 'day' and len(buckets) == 28
    assert start == datetime(2026, 2, 1, tzinfo=zone) and end == datetime(2026, 3, 1, tzinfo=zone)
    assert buckets[0] == start and buckets[-1] == datetime(2026, 2, 28, tzinfo=zone)


def test_march_across_the_dst_change_has_correct_utc_bounds():
    zone = ZoneInfo('America/New_York')
    start, end, granularity, buckets = time_range('2026-03', NOW, zone)
    assert granularity == 'day' and len(buckets) == 31
    assert (utc_iso(start), utc_iso(end)) == ('2026-03-01T05:00:00Z', '2026-04-01T04:00:00Z')
    assert utc_iso(buckets[8]) == '2026-03-09T04:00:00Z'  # The day after clocks spring forward.


def test_current_month_includes_future_days_as_empty_buckets():
    start, end, _, buckets = time_range('2026-09', NOW, timezone.utc)
    points = series([], buckets, 'day', timezone.utc)
    assert len(points) == 30 and all(p['orders'] == 0 and 'month' not in p for p in points)


def test_december_rolls_into_next_year():
    _, end, _, buckets = time_range('2025-12', NOW, timezone.utc)
    assert end == datetime(2026, 1, 1, tzinfo=timezone.utc) and len(buckets) == 31


@pytest.mark.parametrize('range_name, count', [('12m', 12), ('ytd', 9)])
def test_monthly_points_carry_their_local_month(range_name, count):
    zone = ZoneInfo('America/New_York')
    _, _, granularity, buckets = time_range(range_name, NOW, zone)
    points = series([], buckets, granularity, zone)
    assert len(points) == count and points[-1]['month'] == '2026-09'
    assert points[0]['month'] == ('2025-10' if range_name == '12m' else '2026-01')


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    config['accounting'] = dict(config['accounting'], timezone='America/New_York')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    db = connect(config['database'])
    with db:  # 2026-03-01T03:00Z is still Feb 28 in New York; 2026-04-01T03:30Z is still March 31.
        store_orders(db, [ebay_order('A', created='2026-03-10T15:00:00.000Z'),
                          ebay_order('B', created='2026-04-01T03:30:00.000Z'),
                          ebay_order('C', created='2026-03-01T03:00:00.000Z'),
                          ebay_order('D', created='2026-04-01T05:00:00.000Z')], datetime.now(timezone.utc))
    db.close()
    with TestClient(app) as test_client:
        yield test_client


def test_overview_orders_and_export_agree_on_a_month(client):
    overview = client.get('/api/overview?range=2026-03').json()
    listing = client.get('/api/orders?range=2026-03&limit=500').json()
    assert overview['cards']['total_orders'] == listing['total'] == 2
    assert {o['order_id'] for o in listing['orders']} == {'A', 'B'}
    assert len(overview['series']) == 31 and sum(p['orders'] for p in overview['series']) == 2
    export = client.get('/api/orders/export?scope=filtered&range=2026-03')
    assert 'orders-2026-03.csv' in export.headers['content-disposition']
    assert len(export.text.strip().splitlines()) == 3


@pytest.mark.parametrize('bad', ['2099-01', '2026-13', '2026-00', '26-03'])
def test_invalid_or_future_months_are_rejected(client, bad):
    assert client.get(f'/api/overview?range={bad}').status_code == 400
    assert client.get(f'/api/orders?range={bad}').status_code == 400
