import json
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.db.connection import connect
from backend.services.sync.orchestrator import STEPS, SyncContext, run_cycle, save_state
from test_gmail_and_cycle import FakeEbay, FakeGmail

NOW = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)


@pytest.mark.parametrize('name', [name for name, _ in STEPS])
def test_each_step_runs_in_isolation(db, config, name):
    """Regression check: each step runs in isolation.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.
        name: Parameterized sync-step name selected by the test.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    ctx = SyncContext(config, ebay=FakeEbay(), gmail=FakeGmail([]))
    result = run_cycle(ctx, now=NOW, db=db, step_name=name)
    assert set(result) == {name, 'duration_seconds'}
    assert result[name]['ok'], result
    state_names = {r[0] for r in db.execute('SELECT name FROM sync_state')}
    assert state_names == {name}
    assert ctx.current_step is None and not ctx.force


@pytest.mark.parametrize('name', ['returns', 'listings'])
def test_manual_run_bypasses_interval(db, config, name):
    """Regression check: manual run bypasses interval.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.
        name: Parameterized sync-step name selected by the test.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    ctx = SyncContext(config, ebay=FakeEbay(), gmail=FakeGmail([]))
    save_state(db, name, NOW, success=True)
    automatic = dict(STEPS)[name]
    from backend.services.sync.orchestrator import Skipped
    with pytest.raises(Skipped, match='not due'):
        automatic(db, ctx, NOW)
    result = run_cycle(ctx, now=NOW, db=db, step_name=name)
    assert result[name]['ok'] and 'skipped' not in result[name]


def test_individual_history_resumes_and_leaves_completed_backfill_alone(db, config):
    """Regression check: individual history resumes and leaves completed backfill alone.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    calls = []
    class Recording(FakeEbay):
        def raw_orders(self, start, end, date_filter='creationdate'):
            """Record the requested backfill date range without returning new orders.

            Args:
                start: Range start accepted by the mock client, or initial timestamp for a controllable
                    clock.
                end: Range end accepted by the mock client, or optional last recurrence/listing date.
                date_filter: eBay date-filter selector accepted for interface compatibility with the
                    real client.

            Returns:
                List of synthetic Fulfillment orders; recording backfill fakes return an empty list.
            """
            calls.append((start, end))
            return []
    short = config.model_copy(update={'accounting': config.accounting.model_copy(update={'start_date': '2026-06-01'})})
    ctx = SyncContext(short, ebay=Recording())
    run_cycle(ctx, now=NOW, db=db, step_name='orders_backfill')
    cursor = db.execute("SELECT cursor FROM sync_state WHERE name='orders_backfill'").fetchone()[0]
    assert len(calls) == 3 and cursor != 'complete'
    run_cycle(ctx, now=NOW, db=db, step_name='orders_backfill')
    assert calls[3][0] == cursor
    assert db.execute("SELECT cursor FROM sync_state WHERE name='orders_backfill'").fetchone()[0] == 'complete'
    count = len(calls)
    run_cycle(ctx, now=NOW, db=db, step_name='orders_backfill')
    assert len(calls) == count


def test_api_single_step_busy_rejection_and_disabled_loop(tmp_path):
    """Regression check: API single step busy rejection and disabled loop.

    Args:
        tmp_path: Pytest temporary directory for isolated databases, configuration files,
            exports, and backups.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'out'))
    config['sync']['enabled'] = False
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'))
    started, release = threading.Event(), threading.Event()
    class BlockingGmail(FakeGmail):
        def search(self, query):
            """Signal that the Gmail sync entered its search and block until the concurrency test releases it.

            Args:
                query: Gmail search query supplied by the synchronizer; mock implementations validate or
                    ignore it.

            Returns:
                List of configured message identifiers, or an empty list for the blocking concurrency
                test.
            """
            started.set()
            assert release.wait(5)
            return []
    app.state.context._gmail = BlockingGmail([])
    with TestClient(app) as client:
        assert client.post('/api/sync/steps/not-a-step').status_code == 404
        assert client.post('/api/sync/steps/gmail').status_code == 200
        try:
            assert started.wait(3)
            status = client.get('/api/sync/status').json()
            assert status['loop']['running'] and status['loop']['current_step'] == 'gmail'
            assert status['loop']['enabled'] is False
            assert client.post('/api/sync/steps/orders').status_code == 409
            assert client.post('/api/sync/now').status_code == 409
            assert 'webhook' not in json.dumps(status).lower()
        finally:
            release.set()
    db = connect(config['database'])
    assert [r[0] for r in db.execute('SELECT name FROM sync_state')] == ['gmail']
    db.close()


def test_sync_completion_triggers_notification_worker(db, config):
    """Regression check: sync completion triggers notification worker.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    triggered = []
    ctx = SyncContext(config, ebay=FakeEbay())
    ctx.notifications = type('Notifications', (), {'trigger': lambda _: triggered.append(True)})()
    run_cycle(ctx, now=NOW, db=db, step_name='orders')
    assert triggered == [True]
