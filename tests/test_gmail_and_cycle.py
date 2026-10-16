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
    """Regression check: parser extracts matching fields and ignores order id.

    Args:
        None.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    parsed = parse('Ordered: "Lug Nuts 20pc"', AMAZON_HTML)
    assert parsed == {'first_name': 'Jane', 'city': 'Springfield', 'state': 'IL', 'quantity': 1,
                      'category': 'Lug Nuts 20pc', 'grand_total_cents': 2750, 'parse_status': 'parsed',
                      'parse_error': None}
    assert '111-2222222' not in str(parsed)


def test_parser_statuses_for_incomplete_emails():
    """Regression check: parser statuses for incomplete emails.

    Args:
        None.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    assert parse('Ordered: "X" and 2 more items', 'Grand Total: $10.00', False)['parse_status'] == 'partial'
    assert parse('Ordered: "X" and 2 more items', 'Grand Total: $10.00', False)['quantity'] == 3
    assert parse('Your package shipped', '<p>No totals here</p>')['parse_status'] == 'failed'


class FakeGmail:
    def __init__(self, messages):
        """Index the supplied synthetic Gmail messages and initialize the fetched-message audit list.

        Args:
            messages: Synthetic Gmail message payloads indexed by identifier in the mock client.

        Returns:
            None. Initializes state on this mock client instance.
        """
        self.messages = {m['id']: m for m in messages}
        self.fetched = []

    def search(self, query):
        """Validate the Gmail incremental query and return the configured synthetic message identifiers.

        Args:
            query: Gmail search query supplied by the synchronizer; mock implementations validate or
                ignore it.

        Returns:
            List of configured message identifiers, or an empty list for the blocking concurrency
            test.
        """
        assert 'after:' in query
        return list(self.messages)

    def get(self, message_id):
        """Record a mock Gmail message fetch and return its synthetic confirmation payload.

        Args:
            message_id: Synthetic Gmail message identifier used as the purchase record key.

        Returns:
            Gmail-style message dictionary identified by message_id; the quota-limited fake may
            raise instead.
        """
        self.fetched.append(message_id)
        return self.messages[message_id]


class FakeEbay:
    finances_scope_missing = False

    def __init__(self):
        """Initialize the eBay test double with the fixed synthetic order dataset.

        Args:
            None.

        Returns:
            None. Initializes state on this mock client instance.
        """
        self.orders = [ebay_order('A', created='2026-09-24T14:00:00.000Z')]

    def access_token(self):
        """Provide a synthetic eBay seller token so the orchestration tests can check scope without OAuth requests.

        Args:
            None.

        Returns:
            The fixed synthetic token string.
        """
        return 'token'

    def raw_orders(self, start, end, date_filter='creationdate'):
        """Provide synthetic eBay orders through the production-compatible download interface.

        Args:
            start: Range start accepted by the mock client, or initial timestamp for a controllable
                clock.
            end: Range end accepted by the mock client, or optional last recurrence/listing date.
            date_filter: eBay date-filter selector accepted for interface compatibility with the
                real client.

        Returns:
            List of synthetic Fulfillment orders; recording backfill fakes return an empty list.
        """
        return self.orders

    def finance_transactions(self, start, end):
        """Provide one posted synthetic eBay SALE transaction for the orchestration fixture order.

        Args:
            start: Range start accepted by the mock client, or initial timestamp for a controllable
                clock.
            end: Range end accepted by the mock client, or optional last recurrence/listing date.

        Returns:
            List containing the synthetic credited SALE transaction.
        """
        return [sale_transaction('A', 40.0)]

    def returns(self, start, end):
        """Provide an empty return-case response from the eBay test double.

        Args:
            start: Range start accepted by the mock client, or initial timestamp for a controllable
                clock.
            end: Range end accepted by the mock client, or optional last recurrence/listing date.

        Returns:
            Empty list indicating that no synthetic return cases were downloaded.
        """
        return []

    def category(self, item_id):
        """Return a fixed product category from the eBay test double without a Browse API request.

        Args:
            item_id: Synthetic eBay listing identifier whose category or image metadata is
                requested.

        Returns:
            Category breadcrumb string for the synthetic listing.
        """
        return 'eBay Motors > Parts & Accessories'

    def listings(self, observed):
        """Provide synthetic listing snapshots through the production-compatible eBay download interface.

        Args:
            observed: Listing snapshot observation date used by the synthetic listing response.

        Returns:
            List of synthetic listing dictionaries with sold counters and observation dates.
        """
        return [{'item_id': '111', 'title': 'Lug Nuts 20pc M12', 'sku': '', 'sold_quantity': 1,
                 'observed_date': observed, 'status': 'active', 'start_date': '2026-09-01', 'end_date': None}]


def message(message_id, minutes_after=5):
    """Build a synthetic Gmail confirmation at the test reference hour using the shared Amazon HTML fixture.

    Args:
        message_id: Synthetic Gmail message identifier used as the purchase record key.
        minutes_after: Minute of the synthetic confirmation time after the reference order hour.

    Returns:
        Dictionary in the Gmail client message format consumed by store_email.
    """
    stamp = datetime(2026, 9, 24, 14, minutes_after, tzinfo=timezone.utc).timestamp()
    return {'id': message_id, 'internal_date_ms': int(stamp * 1000), 'subject': 'Ordered: "Lug Nuts 20pc"',
            'body': AMAZON_HTML, 'is_html': True}


def test_cycle_syncs_everything_matches_and_never_calls_openai(db, config, monkeypatch):
    """Regression check: cycle syncs everything matches and never calls openai.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    def forbidden(*args, **kwargs):
        """Fail immediately if background synchronization tries to construct an OpenAI client.

        Args:
            args: Positional arguments accepted for compatibility with the production callable being
                mocked.
            kwargs: Keyword arguments forwarded to the mocked callable or used to override synthetic
                payload fields.

        Returns:
            Never returns; raises AssertionError because this path must not call OpenAI.
        """
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
    """Regression check: failing step does not stop the cycle.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    class Broken(FakeEbay):
        def finance_transactions(self, start, end):
            """Simulate an eBay Finances outage to verify that other synchronization steps continue.

            Args:
                start: Range start accepted by the mock client, or initial timestamp for a controllable
                    clock.
                end: Range end accepted by the mock client, or optional last recurrence/listing date.

            Returns:
                Never returns; raises RuntimeError with the synthetic outage message.
            """
            raise RuntimeError('finances down')
    ctx = SyncContext(config.model_copy(update={'gmail': config.gmail.model_copy(update={'enabled': False})}),
                      ebay=Broken())
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert result['finances'] == {'ok': False, 'error': 'finances down'}
    assert result['orders']['ok'] and result['reconcile']['ok']
    assert result['gmail'] == {'ok': True, 'skipped': 'Gmail disabled in config'}
    assert db.execute("SELECT last_error FROM sync_state WHERE name='finances'").fetchone()[0] == 'finances down'


def test_missing_finances_consent_is_reported_not_fatal(db, config):
    """Regression check: missing finances consent is reported not fatal.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    class OldConsent(FakeEbay):
        finances_scope_missing = True
    ctx = SyncContext(config, ebay=OldConsent(), gmail=FakeGmail([]))
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert not result['finances']['ok'] and 'ebay_auth.py' in result['finances']['error']
    assert result['orders']['ok']


@pytest.mark.parametrize('cursor_days', [0])
def test_backfill_is_resumable_and_completes(db, config, cursor_days):
    """Regression check: backfill is resumable and completes.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.
        cursor_days: Parameterized backfill offset; the current case uses zero to start from the
            configured date.

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
    """Regression check: real confirmation layout.

    Args:
        subject: Synthetic Amazon confirmation subject consumed by the email parser.
        recipient: Recipient line inserted into the synthetic Amazon confirmation HTML.
        expected: Expected result for this parametrized input, such as parser fields or a
            formatted profit.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    parsed = parse(subject, REAL_LAYOUT.replace('{recipient}', recipient))
    assert (parsed['first_name'], parsed['city'], parsed['state'], parsed['quantity'], parsed['category']) == expected
    assert parsed['grand_total_cents'] == 7150 and parsed['parse_status'] == 'parsed'
    assert parsed['first_name'] != 'Seller'  # The greeting names the account holder, never the recipient.


def test_stored_emails_are_reparsed_when_the_parser_improves(db):
    """Regression check: stored emails are reparsed when the parser improves.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
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
    """Regression check: Gmail quota errors pause quietly and keep progress.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    class QuotaError(Exception):
        resp = type('Resp', (), {'status': 403})()

        def __str__(self):
            """Expose the synthetic Gmail quota-error text used by the orchestration rate-limit detector.

            Args:
                None.

            Returns:
                String describing the mocked quota exhaustion.
            """
            return "Quota exceeded for quota metric 'Total Query Cost' and limit 'Units per minute per user'"

    class Limited(FakeGmail):
        def get(self, message_id):
            """Record a mock Gmail message fetch and return its synthetic confirmation payload. Raise the synthetic quota error once the configured fetch threshold is reached.

            Args:
                message_id: Synthetic Gmail message identifier used as the purchase record key.

            Returns:
                Gmail-style message dictionary identified by message_id; the quota-limited fake may
                raise instead.
            """
            if len(self.fetched) == 2:
                raise QuotaError()
            return super().get(message_id)

    gmail = Limited([message('m1'), message('m2', 6), message('m3', 7)])
    ctx = SyncContext(config, ebay=FakeEbay(), gmail=gmail)
    result = run_cycle(ctx, now=datetime(2026, 9, 24, 15, tzinfo=timezone.utc), db=db)
    assert result['gmail'] == {'ok': True, 'skipped': 'Gmail rate limit reached after 2 new emails; continuing next cycle'}
    assert db.execute('SELECT count(*) FROM amazon_email_purchases').fetchone()[0] == 2
    assert db.execute("SELECT last_success_at FROM sync_state WHERE name='gmail'").fetchone()[0] is None  # Resumes.
