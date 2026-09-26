import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db import connection
from backend.db.connection import connect
from backend.services.expenses.service import create, expenses_in_range, next_occurrence, occurrences
from backend.services.financials.aggregation import local_zone
from backend.services.sync.ebay_sync import store_orders
from factories import ebay_order

D = date.fromisoformat


def expense(start, recurring=False, end=None):
    return {'start_date': start, 'recurring': recurring, 'end_date': end}


def test_one_time_in_and_out_of_range():
    item = expense('2026-03-15')
    assert occurrences(item, D('2026-03-01'), D('2026-04-01')) == [D('2026-03-15')]
    assert occurrences(item, D('2026-03-16'), D('2026-04-01')) == []
    assert occurrences(item, D('2026-03-01'), D('2026-03-15')) == []  # End is exclusive.
    assert occurrences(item, D('2026-03-15'), D('2026-03-16')) == [D('2026-03-15')]


def test_recurring_clamps_to_short_months_and_leap_years():
    item = expense('2026-01-31', recurring=True)
    assert occurrences(item, D('2026-01-01'), D('2026-05-01')) == [
        D('2026-01-31'), D('2026-02-28'), D('2026-03-31'), D('2026-04-30')]
    leap = expense('2027-12-31', recurring=True)
    assert occurrences(leap, D('2028-02-01'), D('2028-03-01')) == [D('2028-02-29')]
    assert occurrences(expense('2026-01-30', recurring=True), D('2026-02-01'), D('2026-03-01')) == [D('2026-02-28')]


def test_end_date_stops_recurring_inclusive():
    item = expense('2026-01-15', recurring=True, end='2026-03-15')
    assert occurrences(item, D('2026-01-01'), D('2026-12-31')) == [D('2026-01-15'), D('2026-02-15'), D('2026-03-15')]
    stopped = expense('2026-01-15', recurring=True, end='2026-03-14')
    assert occurrences(stopped, D('2026-01-01'), D('2026-12-31'))[-1] == D('2026-02-15')
    assert occurrences(stopped, None, None) == [D('2026-01-15'), D('2026-02-15')]


def test_recurring_starting_mid_range_and_range_starting_mid_month():
    item = expense('2026-03-20', recurring=True)
    assert occurrences(item, D('2026-03-01'), D('2026-06-01')) == [D('2026-03-20'), D('2026-04-20'), D('2026-05-20')]
    assert occurrences(item, D('2026-04-21'), D('2026-05-21')) == [D('2026-05-20')]
    assert occurrences(item, D('2026-01-01'), D('2026-03-20')) == []
    with pytest.raises(ValueError):
        occurrences(item, D('2026-01-01'), None)


def test_all_range_counts_every_occurrence_through_today(db):
    with db:
        create(db, 'Setup', 5000, '2025-11-10', False, None)
        create(db, 'Software', 1000, '2026-01-31', True, None)
        create(db, 'Future', 700, '2026-10-01', False, None)
    # Through 2026-04-30 (exclusive end 05-01): Jan 31, Feb 28, Mar 31, Apr 30 = 4 charges.
    assert expenses_in_range(db, None, D('2026-05-01')) == 5000 + 4 * 1000
    assert expenses_in_range(db, D('2026-02-01'), D('2026-03-01')) == 1000


def test_next_occurrence():
    assert next_occurrence(expense('2026-01-31', recurring=True), D('2026-02-10')) == D('2026-02-28')
    assert next_occurrence(expense('2026-01-31', recurring=True), D('2026-03-01')) == D('2026-03-31')
    assert next_occurrence(expense('2026-01-15', recurring=True, end='2026-02-20'), D('2026-02-16')) is None
    assert next_occurrence(expense('2026-12-05', recurring=True), D('2026-09-25')) == D('2026-12-05')
    assert next_occurrence(expense('2026-09-25'), D('2026-09-25')) == D('2026-09-25')
    assert next_occurrence(expense('2026-09-24'), D('2026-09-25')) is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'output'))
    config['gmail'] = dict(config['gmail'], token_path=str(tmp_path / 'missing-token.json'))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)
    db = connect(config['database'])
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    with db:
        store_orders(db, [ebay_order('A', created=created), ebay_order('B', created=created, name='Bob')],
                     datetime.now(timezone.utc))
    db.close()
    with TestClient(app) as test_client:
        test_client.today = datetime.now(timezone.utc).astimezone(local_zone(config['accounting']['timezone'])).date()
        yield test_client


def test_crud_through_the_api(client):
    today = client.today
    empty = client.get('/api/expenses').json()
    empty.pop('ordering_fees')
    assert empty == {'expenses': [], 'this_month_cents': 0, 'monthly_recurring_cents': 0}
    made = client.post('/api/expenses', json={'name': ' Shopify ', 'amount': '$1,234.56', 'date': today.isoformat(),
                                              'recurring': True})
    assert made.status_code == 200
    item = made.json()
    assert item == {'id': item['id'], 'name': 'Shopify', 'amount_cents': 123456, 'start_date': today.isoformat(),
                    'recurring': True, 'end_date': None, 'next_occurrence': today.isoformat()}
    one_time = client.post('/api/expenses', json={'name': 'Label printer', 'amount': 50, 'date': today.isoformat()}).json()
    assert (one_time['amount_cents'], one_time['recurring']) == (5000, False)
    listing = client.get('/api/expenses').json()
    assert len(listing['expenses']) == 2
    assert (listing['this_month_cents'], listing['monthly_recurring_cents']) == (128456, 123456)
    stopped = client.put(f'/api/expenses/{item["id"]}', json={'name': 'Shopify', 'amount': '1234.56',
                                                               'date': today.isoformat(), 'recurring': True,
                                                               'end_date': today.isoformat()}).json()
    assert stopped['end_date'] == today.isoformat() and stopped['next_occurrence'] == today.isoformat()
    ended = client.put(f'/api/expenses/{item["id"]}', json={'name': 'Shopify', 'amount': '9.99',
                                                             'date': (today - timedelta(days=40)).isoformat(),
                                                             'recurring': True,
                                                             'end_date': (today - timedelta(days=1)).isoformat()}).json()
    assert ended['amount_cents'] == 999 and ended['next_occurrence'] is None
    assert client.delete(f'/api/expenses/{one_time["id"]}').json() == {'deleted': one_time['id']}
    assert client.delete(f'/api/expenses/{one_time["id"]}').status_code == 404
    assert client.put('/api/expenses/9999', json={'name': 'x', 'amount': '1', 'date': '2026-01-01'}).status_code == 404
    assert [e['id'] for e in client.get('/api/expenses').json()['expenses']] == [item['id']]


@pytest.mark.parametrize('body', [
    {'name': '  ', 'amount': '5', 'date': '2026-01-01'},
    {'name': 'x', 'amount': '0', 'date': '2026-01-01'},
    {'name': 'x', 'amount': '-3.00', 'date': '2026-01-01'},
    {'name': 'x', 'amount': '0.001', 'date': '2026-01-01'},
    {'name': 'x', 'amount': 'abc', 'date': '2026-01-01'},
    {'name': 'x', 'amount': 'NaN', 'date': '2026-01-01'},
    {'name': 'x', 'amount': '5', 'date': '2026-02-30'},
    {'name': 'x', 'amount': '5', 'date': '01/02/2026'},
    {'name': 'x', 'amount': '5', 'date': ''},
    {'name': 'x', 'amount': '5', 'date': '2026-03-01', 'recurring': True, 'end_date': '2026-02-28'},
    {'name': 'x', 'amount': '5', 'date': '2026-03-01', 'end_date': 'soon'},
])
def test_validation_returns_400(client, body):
    assert client.post('/api/expenses', json=body).status_code == 400
    made = client.post('/api/expenses', json={'name': 'ok', 'amount': '1', 'date': '2026-01-01'}).json()
    assert client.put(f'/api/expenses/{made["id"]}', json=body).status_code == 400


def test_overview_cards_subtract_expenses_only_from_profit_after(client):
    client.post('/api/orders/A/amazon-cost', json={'amount': '20.00'})
    before = client.get('/api/overview?range=30d').json()['cards']
    assert before['expenses_cents'] == 0 and before['profit_after_expenses_cents'] == before['profit_cents']
    today = client.today
    client.post('/api/expenses', json={'name': 'Supplies', 'amount': '10.00', 'date': today.isoformat()})
    client.post('/api/expenses', json={'name': 'Old', 'amount': '5.00', 'date': (today - timedelta(days=60)).isoformat()})
    client.post('/api/expenses', json={'name': 'Later', 'amount': '7.00', 'date': (today + timedelta(days=1)).isoformat()})
    after = client.get('/api/overview?range=30d').json()
    cards = after['cards']
    assert cards['expenses_cents'] == 1000
    assert cards['profit_after_expenses_cents'] == before['profit_cents'] - 1000
    unchanged = ('profit_cents', 'profit_margin', 'avg_profit_per_order_cents', 'prime_cashback_cents', 'net_revenue_cents')
    assert {k: cards[k] for k in unchanged} == {k: before[k] for k in unchanged}
    assert all('expenses' not in key for point in after['series'] for key in point)
    assert client.get('/api/overview?range=all').json()['cards']['expenses_cents'] == 1500  # Through today only.


def test_migration_adds_expenses_to_an_old_database_with_a_backup(tmp_path, monkeypatch):
    path = tmp_path / 'app.sqlite3'
    monkeypatch.setattr(connection, 'MIGRATIONS', connection.MIGRATIONS[:2])
    old = connect(str(path))
    with old:
        old.execute("INSERT INTO weeks VALUES ('2026-09-14', '{}', '2026-09-14')")
    old.close()
    assert not (tmp_path / 'backups').exists()
    monkeypatch.undo()
    db = connect(str(path))
    assert [r[0] for r in db.execute('SELECT version FROM schema_migrations')] == [1, 2, 3]
    assert db.execute('SELECT count(*) FROM expenses').fetchone()[0] == 0
    assert db.execute('SELECT count(*) FROM weeks').fetchone()[0] == 1
    backups = list((tmp_path / 'backups').iterdir())
    assert len(backups) == 1
    saved = sqlite3.connect(backups[0])
    assert not saved.execute("SELECT count(*) FROM sqlite_master WHERE name='expenses'").fetchone()[0]
    saved.close()
    db.close()
    connect(str(path)).close()
    assert len(list((tmp_path / 'backups').iterdir())) == 1


def test_current_month_from_picker_and_current_month_button_agree(tmp_path, monkeypatch):
    """Future charges later this month never reduce profit, however the month is selected."""
    import json
    from datetime import date, datetime, timezone

    from fastapi.testclient import TestClient

    from backend.api import deps
    from backend.app import create_app
    fixed = datetime(2026, 9, 15, 16, tzinfo=timezone.utc)
    monkeypatch.setattr(deps, 'now', lambda: fixed)
    monkeypatch.setattr('backend.api.overview.now', lambda: fixed)
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'out'))
    config['accounting'] = dict(config['accounting'], timezone='America/New_York')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with TestClient(create_app(str(path), env_path=str(tmp_path / 'none.env'), start_loop=False)) as client:
        for body in ({'name': 'Gas', 'amount': '40', 'date': '2026-09-10', 'recurring': False},
                     {'name': 'Tool', 'amount': '29.99', 'date': '2026-08-31', 'recurring': True},
                     {'name': 'Later', 'amount': '5', 'date': '2026-09-20', 'recurring': False}):
            assert client.post('/api/expenses', json=body).status_code == 200
        button = client.get('/api/overview?range=month').json()['cards']['expenses_cents']
        picker = client.get('/api/overview?range=2026-09').json()['cards']['expenses_cents']
        assert button == picker == 4000  # Sep 30 subscription and Sep 20 expense are still ahead.
        assert client.get('/api/overview?range=2026-08').json()['cards']['expenses_cents'] == 2999


def add_orders(client, orders):
    db = connect(client.app.state.config.database)
    with db:
        store_orders(db, orders, datetime.now(timezone.utc))
    db.close()


def test_ordering_fees_count_every_order_including_cancelled(client):
    stamp = lambda moment: moment.replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    now = datetime.now(timezone.utc)
    last_year = datetime(client.today.year - 1, 7, 1, 17, tzinfo=timezone.utc)
    add_orders(client, [ebay_order('C', created=stamp(now), cancelled=True), ebay_order('D', created=stamp(last_year))])
    fees = client.get('/api/expenses').json()['ordering_fees']
    per = fees['per_order_cents']
    assert per == getattr(client.app.state.config.accounting, 'ordering_fee_cents', 30)
    assert (fees['this_month_count'], fees['ytd_count'], fees['all_count']) == (3, 3, 4)  # A, B and cancelled C.
    assert (fees['this_month_cents'], fees['ytd_cents'], fees['all_cents']) == (3 * per, 3 * per, 4 * per)


def test_ordering_fees_never_change_expenses_or_profit_after_expenses(client):
    client.post('/api/orders/A/amazon-cost', json={'amount': '20.00'})
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    add_orders(client, [ebay_order('C', created=created, cancelled=True)])
    cards = client.get('/api/overview?range=30d').json()['cards']
    assert cards['expenses_cents'] == 0 and cards['profit_after_expenses_cents'] == cards['profit_cents']
    client.post('/api/expenses', json={'name': 'Supplies', 'amount': '10.00', 'date': client.today.isoformat()})
    listing = client.get('/api/expenses').json()
    assert listing['ordering_fees']['this_month_count'] == 3
    assert listing['this_month_cents'] == 1000  # The fee is not an expense entry.
    after = client.get('/api/overview?range=30d').json()['cards']
    assert after['expenses_cents'] == 1000
    assert after['profit_after_expenses_cents'] == after['profit_cents'] - 1000
