from datetime import datetime, timezone

import pytest

from backend.integrations.gmail.amazon_parser import parse
from backend.services.sync.orchestrator import SyncContext, run_cycle
from factories import ebay_order, sale_transaction

AMAZON_HTML = '''<html><body><p>Hello,</p><p>Thanks for your order.</p>
<table><tr><td>Ship to: JANE DOE - SPRINGFIELD, IL 62704</td></tr>
<tr><td>Lug Nuts 20pc</td><td>Quantity: 1</td></tr></table>
<p>Order # 111-2222222-3333333</p><p>Grand Total: $27.50</p></body></html>'''


def test_parser_extracts_matching_fields_and_ignores_order_id():
    parsed = parse('Ordered: "Lug Nuts 20pc"', AMAZON_HTML)
    assert parsed == {'first_name': 'Jane', 'city': 'Springfield', 'state': 'IL', 'quantity': 1,
                      'category': 'Lug Nuts 20pc', 'grand_total_cents': 2750, 'parse_status': 'parsed',
                      'parse_error': None}
    assert '111-2222222' not in str(parsed)


def test_parser_statuses_for_incomplete_emails():
    assert parse('Ordered: "X" and 2 more items', 'Grand Total: $10.00', False)['parse_status'] == 'partial'
    assert parse('Ordered: "X" and 2 more items', 'Grand Total: $10.00', False)['quantity'] == 3
    assert parse('Your package shipped', '<p>No totals here</p>')['parse_status'] == 'failed'


class FakeGmail:
    def __init__(self, messages):
        self.messages = {m['id']: m for m in messages}
        self.fetched = []

    def search(self, query):
        assert 'after:' in query
        return list(self.messages)

    def get(self, message_id):
        self.fetched.append(message_id)
        return self.messages[message_id]


class FakeEbay:
    finances_scope_missing = False

    def __init__(self):
        self.orders = [ebay_order('A', created='2026-09-24T14:00:00.000Z')]

    def access_token(self):
        return 'token'

    def raw_orders(self, start, end, date_filter='creationdate'):
        return self.orders

    def finance_transactions(self, start, end):
        return [sale_transaction('A', 40.0)]

    def returns(self, start, end):
        return []

    def category(self, item_id):
        return 'eBay Motors > Parts & Accessories'

    def listings(self, observed):
        return [{'item_id': '111', 'title': 'Lug Nuts 20pc M12', 'sku': '', 'sold_quantity': 1,
                 'observed_date': observed, 'status': 'active', 'start_date': '2026-09-01', 'end_date': None}]


def message(message_id, minutes_after=5):
    stamp = datetime(2026, 9, 24, 14, minutes_after, tzinfo=timezone.utc).timestamp()
    return {'id': message_id, 'internal_date_ms': int(stamp * 1000), 'subject': 'Ordered: "Lug Nuts 20pc"',
            'body': AMAZON_HTML, 'is_html': True}


def test_cycle_syncs_everything_matches_and_never_calls_openai(db, config, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('The sync loop must never call OpenAI')
    monkeypatch.setattr('openai.OpenAI', forbidden)
    gmail = FakeGmail([message('m1')])
    ctx = SyncContext(config, ebay=FakeEbay(), gmail=gmail)
    now = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)
    result = run_cycle(ctx, now=now, db=db)
    assert all(step['ok'] for name, step in result.items() if name != 'duration_seconds'), result
    assert result['reconcile'] == {'ok': True, 'MATCHED': 1}
    assert db.execute('SELECT category FROM listings WHERE item_id=?', ('111',)).fetchone()[0].startswith('eBay Motors')
    # Second cycle: the same Gmail search result is not fetched or stored again.
    run_cycle(ctx, now=now.replace(minute=5), db=db)
    assert gmail.fetched == ['m1']
    assert db.execute('SELECT count(*) FROM amazon_email_purchases').fetchone()[0] == 1
    assert db.execute('SELECT count(*) FROM orders').fetchone()[0] == 1


def test_failing_step_does_not_stop_the_cycle(db, config):
    class Broken(FakeEbay):
        def finance_transactions(self, start, end):
            raise RuntimeError('finances down')
    ctx = SyncContext(config.model_copy(update={'gmail': config.gmail.model_copy(update={'enabled': False})}),
                      ebay=Broken())
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert result['finances'] == {'ok': False, 'error': 'finances down'}
    assert result['orders']['ok'] and result['reconcile']['ok']
    assert result['gmail'] == {'ok': True, 'skipped': 'Gmail disabled in config'}
    assert db.execute("SELECT last_error FROM sync_state WHERE name='finances'").fetchone()[0] == 'finances down'


def test_missing_finances_consent_is_reported_not_fatal(db, config):
    class OldConsent(FakeEbay):
        finances_scope_missing = True
    ctx = SyncContext(config, ebay=OldConsent(), gmail=FakeGmail([]))
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert not result['finances']['ok'] and 'ebay_auth.py' in result['finances']['error']
    assert result['orders']['ok']


@pytest.mark.parametrize('cursor_days', [0])
def test_backfill_is_resumable_and_completes(db, config, cursor_days):
    calls = []

    class Recording(FakeEbay):
        def raw_orders(self, start, end, date_filter='creationdate'):
            calls.append((date_filter, start[:10], end[:10]))
            return []
    short = config.model_copy(update={'accounting': config.accounting.model_copy(update={'start_date': '2026-06-01'}),
                                      'gmail': config.gmail.model_copy(update={'enabled': False})})
    ctx = SyncContext(short, ebay=Recording())
    now = datetime(2026, 9, 24, 15, tzinfo=timezone.utc)
    run_cycle(ctx, now=now, db=db)
    backfill = [c for c in calls if c[0] == 'creationdate']
    assert backfill[0][1] == '2026-06-01' and len(backfill) == 3  # 3 chunks of 30 days per cycle
    run_cycle(ctx, now=now, db=db)
    assert db.execute("SELECT cursor FROM sync_state WHERE name='orders_backfill'").fetchone()[0] == 'complete'
    calls.clear()
    run_cycle(ctx, now=now, db=db)
    assert [c[0] for c in calls] == ['lastmodifieddate']


# Anonymized copy of the seller's real 2026 confirmation layout: a style block, invisible preheader
# padding, the account holder's greeting (not the recipient) and a "First - CITY, ST" line.
REAL_LAYOUT = '''<html><head><style>.mj-column-per-100{width:100%!important}</style></head><body>
<div>Ordered 2 items: Automotive͏ ‌ ­͏ ‌ ­</div>
<div>Your Orders</div><div>Your Account</div><div>Seller, thanks for your order!</div>
<div>Ordered</div><div>Shipped</div><div>Arriving Monday</div><div>2 Automotive items</div>
<div>{recipient}</div><div>Order #</div><div>‫123-4567890-1234567</div>
<div>View or edit order</div><div>Grand Total:</div><div> $71.50</div>
<div>©2026 Amazon.com, Inc., 410 Terry Avenue N., Seattle, WA 98109.</div></body></html>'''


@pytest.mark.parametrize('subject,recipient,expected', [
    ('Ordered 2 items: Automotive', 'Jordan - SPOKANE, WA', ('Jordan', 'Spokane', 'WA', 2, 'Automotive')),
    ('Ordered: ⁦2⁩ Health Care items', 'Renée - SALT LAKE CITY, UT', ('Renée', 'Salt Lake City', 'UT', 2, 'Health Care')),
    ('Ordered 1 item: Supplements', 'dAN - EAST ORANGE, NJ', ('Dan', 'East Orange', 'NJ', 1, 'Supplements')),
])
def test_real_confirmation_layout(subject, recipient, expected):
    parsed = parse(subject, REAL_LAYOUT.replace('{recipient}', recipient))
    assert (parsed['first_name'], parsed['city'], parsed['state'], parsed['quantity'], parsed['category']) == expected
    assert parsed['grand_total_cents'] == 7150 and parsed['parse_status'] == 'parsed'
    assert parsed['first_name'] != 'Seller'  # The greeting names the account holder, never the recipient.


def test_stored_emails_are_reparsed_when_the_parser_improves(db):
    from backend.services.sync.orchestrator import reparse_emails
    body = REAL_LAYOUT.replace('{recipient}', 'Jordan - SPOKANE, WA')
    with db:
        db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, grand_total_cents,
                      parse_status, parse_error, body, fetched_at) VALUES ('m1','2026-09-24T12:00:00Z',
                      'Ordered 2 items: Automotive', 7150, 'partial', 'Recipient not found', ?, '2026-09-24T12:00:00Z')''',
                   (body,))
    assert reparse_emails(db) == 1
    row = db.execute('SELECT first_name, city, state, quantity, parse_status, body FROM amazon_email_purchases').fetchone()
    assert tuple(row) == ('Jordan', 'Spokane', 'WA', 2, 'parsed', None)  # Body dropped once parsed.
    assert reparse_emails(db) == 0


def test_gmail_quota_errors_pause_quietly_and_keep_progress(db, config):
    class QuotaError(Exception):
        resp = type('Resp', (), {'status': 403})()

        def __str__(self):
            return "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'"

    class Limited(FakeGmail):
        def get(self, message_id):
            if len(self.fetched) == 2:
                raise QuotaError()
            return super().get(message_id)

    gmail = Limited([message('m1'), message('m2', 6), message('m3', 7)])
    ctx = SyncContext(config, ebay=FakeEbay(), gmail=gmail)
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert result['gmail'] == {'ok': True, 'skipped': 'Gmail rate limit reached after 2 new emails; continuing next cycle'}
    assert db.execute('SELECT count(*) FROM amazon_email_purchases').fetchone()[0] == 2
    assert db.execute("SELECT last_success_at FROM sync_state WHERE name='gmail'").fetchone()[0] is None  # Resumes.
