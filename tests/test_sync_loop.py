import asyncio
import sqlite3
import threading
import time
from datetime import datetime, timezone

from backend.db.connection import KEEP_BACKUPS, backup_database
from backend.services.sync.loop import SyncLoop
from backend.services.sync.orchestrator import store_email

AMAZON = ('<p>Ship to: JANE DOE - SPRINGFIELD, IL 62704</p><p>Order # 111-2222222-3333333</p>'
          '<p>Quantity: 1</p><p>Grand Total: $27.50</p>')


def test_loop_runs_cycles_wakes_early_and_never_overlaps():
    calls, active, overlaps = [], [0], []

    def cycle():
        active[0] += 1
        overlaps.append(active[0] > 1)
        time.sleep(0.05)
        calls.append(threading.current_thread().name)
        active[0] -= 1
        return {'ok': True}

    async def scenario():
        loop = SyncLoop(cycle, interval_seconds=3600)
        loop.start()
        await asyncio.sleep(0.2)  # First cycle runs immediately on start.
        assert len(calls) == 1
        assert await asyncio.gather(loop.run_once(), loop.run_once()) in ([{'ok': True}, None], [None, {'ok': True}])
        threading.Thread(target=loop.trigger).start()  # "Sync Now" from an API worker thread.
        await asyncio.sleep(0.3)
        await loop.stop()
        return loop

    loop = asyncio.run(scenario())
    assert len(calls) == 3 and not any(overlaps)
    assert all(name != threading.main_thread().name for name in calls)  # Cycles never block the event loop.
    assert loop.status()['last_result'] == {'ok': True} and not loop.status()['running']


def test_sync_now_runs_a_cycle_when_background_sync_is_disabled(tmp_path, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from backend.app import create_app
    config = json.loads(open('config.json').read())
    config.update(database=str(tmp_path / 'app.sqlite3'), output_dir=str(tmp_path / 'out'))
    config['sync'] = dict(config['sync'], enabled=False)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    ran = []
    app = create_app(str(path), env_path=str(tmp_path / 'none.env'))
    app.state.loop.cycle = lambda: ran.append(1) or {'ok': True}
    with TestClient(app) as client:
        assert client.get('/api/sync/status').json()['loop']['enabled'] is False
        client.post('/api/sync/now')
        for _ in range(50):
            if ran:
                break
            time.sleep(0.02)
    assert ran == [1]


def test_parsed_emails_keep_no_body_and_order_numbers_are_redacted(db):
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    received = int(now.timestamp() * 1000)
    store_email(db, {'id': 'parsed', 'internal_date_ms': received, 'subject': 'Ordered: "Lug Nuts"',
                     'body': AMAZON, 'is_html': True}, now)
    store_email(db, {'id': 'partial', 'internal_date_ms': received, 'subject': 'Ordered: "Mat"',
                     'body': 'Order # 111-2222222-3333333 Grand Total: $9.00', 'is_html': False}, now)
    bodies = dict(db.execute('SELECT gmail_message_id, body FROM amazon_email_purchases').fetchall())
    assert bodies['parsed'] is None
    assert '111-2222222-3333333' not in bodies['partial'] and 'order number removed' in bodies['partial']


def test_only_the_newest_backups_are_kept(tmp_path):
    path = tmp_path / 'app.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE t (x)')
    for _ in range(KEEP_BACKUPS + 3):
        backup_database(db, path)
    db.close()
    assert len(list((tmp_path / 'backups').iterdir())) == KEEP_BACKUPS
