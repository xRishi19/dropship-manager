import types

import httpx
import pytest
from datetime import datetime, timezone

from backend.config import EbayConfig
from backend.integrations.ebay import client as ebay_client
from backend.integrations.ebay.client import EbayClient, EbayError
from backend.services.sync.ebay_sync import sync


def order(order_id, qty=3, canceled=False):
    """Build a minimal Fulfillment order with one product line and configurable cancellation and unit count.

    Args:
        order_id: Synthetic eBay or Amazon order identifier used to distinguish test records.
        qty: Units on the synthetic eBay order line.
        canceled: Whether the synthetic eBay order payload reports cancellation.

    Returns:
        Synthetic eBay order dictionary for pagination and sourcing-ingestion tests.
    """
    return {'orderId': order_id, 'creationDate': '2026-09-01T12:00:00Z',
            'cancelStatus': {'cancelState': 'CANCELED' if canceled else 'NONE_REQUESTED'},
            'lineItems': [{'lineItemId': order_id + '-line', 'legacyItemId': '123',
                           'title': 'garage door cable', 'quantity': qty, 'sku': 'cable'}]}


def xml(section, page=1, pages=1, total=1, item='123', ack='Success'):
    """Render a complete synthetic GetMyeBaySelling XML response with controllable pagination and acknowledgement metadata.

    Args:
        section: Trading API listing container name, such as ActiveList or UnsoldList.
        page: Current listing XML pagination page number.
        pages: Total listing XML pagination page count.
        total: TotalNumberOfEntries value reported by the synthetic Trading API page.
        item: Synthetic listing identifier, or listing identifier inserted into the XML fixture.
        ack: Trading API acknowledgement value used to test success and incomplete-response
            handling.

    Returns:
        XML string containing one listing and the requested Trading API response section.
    """
    return f'''<GetMyeBaySellingResponse xmlns="urn:ebay:apis:eBLBaseComponents">
    <Ack>{ack}</Ack><{section}><PaginationResult><TotalNumberOfPages>{pages}</TotalNumberOfPages>
    <TotalNumberOfEntries>{total}</TotalNumberOfEntries></PaginationResult><ItemArray><Item>
    <ItemID>{item}</ItemID><Title>garage door cable</Title><SKU>cable</SKU>
    <Quantity>900</Quantity><SellingStatus><QuantitySold>8</QuantitySold></SellingStatus>
    <ListingDetails><StartTime>2026-08-01T10:00:00.000Z</StartTime><EndTime>2026-10-01T10:00:00.000Z</EndTime></ListingDetails>
    </Item></ItemArray></{section}></GetMyeBaySellingResponse>'''


def test_oauth_refresh_and_paginated_orders(monkeypatch):
    """Regression check: OAuth refresh and paginated orders.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    monkeypatch.setenv('EBAY_REFRESH_TOKEN', 'secret-refresh')
    monkeypatch.setenv('EBAY_CLIENT_ID', 'id')
    monkeypatch.setenv('EBAY_CLIENT_SECRET', 'secret')
    requests = []
    def handler(request):
        """Record mock OAuth and Fulfillment requests, validate the seller token, and supply paginated synthetic orders.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX Response with the synthetic OAuth JSON, order JSON, or listing XML for the
            intercepted request.
        """
        requests.append(request)
        if request.url.path.endswith('/token'):
            assert b'grant_type=refresh_token' in request.content
            return httpx.Response(200, json={'access_token': 'seller-token', 'expires_in': 7200})
        assert request.headers['Authorization'] == 'Bearer seller-token'
        offset = int(request.url.params['offset'])
        return httpx.Response(200, json={'orders': [order(str(offset), canceled=offset == 1)], 'total': 2})
    api = EbayClient(EbayConfig(), httpx.Client(transport=httpx.MockTransport(handler)))
    rows = api.orders('2026-09-01T00:00:00Z', '2026-09-23T00:00:00Z')
    assert [r['quantity'] for r in rows] == [3, 0]
    assert len(requests) == 3
    assert all('buyer' not in r for r in rows)


def test_active_unsold_listings_pagination_and_parent_quantity(monkeypatch):
    """Regression check: active unsold listings pagination and parent quantity.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    monkeypatch.setenv('EBAY_ACCESS_TOKEN', 'seller')
    monkeypatch.delenv('EBAY_REFRESH_TOKEN', raising=False)
    calls = []
    def handler(request):
        """Record mock Trading API requests, validate authorization, and supply active/unsold listing pages.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX Response with the synthetic OAuth JSON, order JSON, or listing XML for the
            intercepted request.
        """
        calls.append(request)
        assert request.headers['X-EBAY-API-IAF-TOKEN'] == 'seller'
        if b'<ActiveList>' in request.content:
            page = 2 if b'<PageNumber>2' in request.content else 1
            return httpx.Response(200, text=xml('ActiveList', page, 2, 2, str(page)))
        assert b'<DurationInDays>60' in request.content
        return httpx.Response(200, text=xml('UnsoldList', item='3'))
    api = EbayClient(EbayConfig(), httpx.Client(transport=httpx.MockTransport(handler)))
    rows = api.listings('2026-09-23')
    assert len(rows) == 3
    assert len(calls) == 3
    assert {r['sold_quantity'] for r in rows} == {8}
    assert rows[-1]['status'] == 'ended_unsold'
    assert {r['start_date'] for r in rows} == {'2026-08-01'}
    # Active listings report a scheduled end, which is not a real end date.
    assert [r['end_date'] for r in rows] == [None, None, '2026-10-01']


@pytest.mark.parametrize('body', [xml('ActiveList', ack='Failure'), xml('ActiveList', total=25000), '<root/>'])
def test_incomplete_listing_download_is_rejected(monkeypatch, body):
    """Regression check: incomplete listing download is rejected.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        body: Synthetic HTTP, email, or API request payload used to exercise the current test
            case.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    monkeypatch.setenv('EBAY_ACCESS_TOKEN', 'seller')
    monkeypatch.delenv('EBAY_REFRESH_TOKEN', raising=False)
    api = EbayClient(EbayConfig(), httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=body))))
    with pytest.raises(EbayError):
        api.listings('2026-09-23')


def test_api_errors_do_not_expose_credentials(monkeypatch):
    """Regression check: API errors do not expose credentials.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    monkeypatch.setenv('EBAY_ACCESS_TOKEN', 'SECRET')
    monkeypatch.delenv('EBAY_REFRESH_TOKEN', raising=False)
    api = EbayClient(EbayConfig(), httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, text='SECRET'))))
    with pytest.raises(EbayError, match='401') as error:
        api.orders('start', 'end')
    assert 'SECRET' not in str(error.value)


def test_sync_atomic_and_repeat_safe_and_cancellation_updates(db, config):
    """Regression check: sync atomic and repeat safe and cancellation updates.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        config: Validated application configuration fixture with a temporary output directory.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    class API:
        canceled = False
        fail = False
        def raw_orders(self, *args):
            """Provide synthetic eBay orders through the production-compatible download interface.

            Args:
                args: Positional arguments accepted for compatibility with the production callable being
                    mocked.

            Returns:
                List of synthetic Fulfillment orders; recording backfill fakes return an empty list.
            """
            return [order('order', canceled=self.canceled)]
        def listings(self, *args):
            """Provide synthetic listing snapshots through the production-compatible eBay download interface. Raise EbayError when the test requests an incomplete-download failure.

            Args:
                args: Positional arguments accepted for compatibility with the production callable being
                    mocked.

            Returns:
                List of synthetic listing dictionaries with sold counters and observation dates.
            """
            if self.fail:
                raise EbayError('Failure on final page')
            return [{'item_id': '123', 'title': 'garage door cable', 'sku': 'cable',
                     'sold_quantity': 100, 'observed_date': '2026-09-23', 'status': 'active'}]
    api = API()
    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    sync(db, config, api, now)
    sync(db, config, api, now)
    assert db.execute('SELECT sum(quantity) FROM sales').fetchone()[0] == 3
    api.canceled = True
    api.fail = True
    with pytest.raises(EbayError):
        sync(db, config, api, now)
    assert db.execute('SELECT sum(quantity) FROM sales').fetchone()[0] == 3
    api.fail = False
    sync(db, config, api, now)
    assert db.execute('SELECT sum(quantity) FROM sales').fetchone()[0] == 0


# --- OAuth token expiry and 401 recovery -------------------------------------------------------

def refresh_env(monkeypatch):
    """Replace eBay OAuth environment values with synthetic credentials and remove any direct seller access token.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. The monkeypatch fixture restores the original environment after the test.
    """
    monkeypatch.setenv('EBAY_REFRESH_TOKEN', 'secret-refresh')
    monkeypatch.setenv('EBAY_CLIENT_ID', 'id')
    monkeypatch.setenv('EBAY_CLIENT_SECRET', 'secret')
    monkeypatch.delenv('EBAY_ACCESS_TOKEN', raising=False)


def wall_clock(monkeypatch, start=1_000_000.0):
    """Replace the client's `time` module: wall clock is controllable, sleep is a no-op, and any
    use of time.monotonic() fails the test (it stops while macOS sleeps).

    Install a mutable wall clock and no-op sleep in the eBay client; reject monotonic-clock use to expose token-expiry bugs.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        start: Initial Unix wall-clock timestamp in seconds, defaulting to 1,000,000.

    Returns:
        SimpleNamespace with a mutable now attribute controlling mocked wall-clock time.
    """
    def monotonic():
        """Reject use of a monotonic clock for OAuth expiry calculations in the wall-clock regression test.

        Args:
            None.

        Returns:
            Never returns; raises AssertionError when called.
        """
        raise AssertionError('token expiry must not depend on time.monotonic()')
    clock = types.SimpleNamespace(now=start)
    fake = types.SimpleNamespace(time=lambda: clock.now, sleep=lambda seconds: None, monotonic=monotonic)
    monkeypatch.setattr(ebay_client, 'time', fake)
    return clock


class TokenServer:
    """Token endpoint issuing token-1, token-2, ...; API endpoints answer with `api(request)`."""
    def __init__(self, api, token_status=200):
        """Configure the mocked eBay HTTP dispatcher and initialize separate token/API request audit lists.

        Args:
            api: Callable MockTransport dispatcher for requests outside the OAuth token endpoint.
            token_status: HTTP status returned by the mocked OAuth token endpoint.

        Returns:
            None. Initializes state on this mock client instance.
        """
        self.api, self.token_status = api, token_status
        self.token_calls, self.api_calls = [], []

    def __call__(self, request):
        """Dispatch a mock eBay HTTP request, recording token calls separately from authorized API calls.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            Synthetic OAuth HTTP response or the response produced by the configured API callback.
        """
        if request.url.path.endswith('/identity/v1/oauth2/token'):
            self.token_calls.append(request)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={'error': 'invalid_client'})
            return httpx.Response(200, json={'access_token': f'token-{len(self.token_calls)}', 'expires_in': 7200})
        self.api_calls.append(request)
        return self.api(request)

    def client(self):
        """Construct an eBay client using this callable fake as its HTTP transport.

        Args:
            None.

        Returns:
            EbayClient instance backed by an HTTPX MockTransport.
        """
        return EbayClient(EbayConfig(), httpx.Client(transport=httpx.MockTransport(self)))


def orders_ok(request):
    """Return one synthetic order as a successful paginated Fulfillment API response.

    Args:
        request: HTTPX request intercepted by a MockTransport; no live service is contacted.

    Returns:
        HTTPX Response with status 200, one order, and total equal to one.
    """
    return httpx.Response(200, json={'orders': [order('1')], 'total': 1})


def test_cached_token_is_refreshed_after_wall_clock_expiry(monkeypatch):
    """Regression check: cached token is refreshed after wall clock expiry.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    clock = wall_clock(monkeypatch)
    server = TokenServer(orders_ok)
    api = server.client()
    assert api.access_token() == 'token-1'
    assert api.access_token() == 'token-1'  # cached while valid
    assert len(server.token_calls) == 1
    clock.now += 3 * 3600  # Mac slept for 3 hours; the 2-hour token has expired
    assert api.access_token() == 'token-2'
    assert len(server.token_calls) == 2
    api.raw_orders('start', 'end')
    assert server.api_calls[-1].headers['Authorization'] == 'Bearer token-2'
    assert len(server.token_calls) == 2


def test_app_token_is_refreshed_after_wall_clock_expiry(monkeypatch):
    """Regression check: app token is refreshed after wall clock expiry.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    clock = wall_clock(monkeypatch)
    server = TokenServer(orders_ok)
    api = server.client()
    assert api.app_token() == 'token-1'
    assert api.app_token() == 'token-1'
    clock.now += 3 * 3600
    assert api.app_token() == 'token-2'
    assert len(server.token_calls) == 2
    assert b'grant_type=client_credentials' in server.token_calls[-1].content


def reject_token(bad, good_response, header='Authorization', prefix='Bearer '):
    """Create a mock HTTP dispatcher that rejects an old token and delegates requests using the refreshed token.

    Args:
        bad: Token or range string that should be rejected by the implementation under test.
        good_response: Callback producing the successful mocked response after token refresh.
        header: HTTP header name checked by the fake authorization dispatcher.
        prefix: Text prepended to the authorization token before checking request headers.

    Returns:
        Callable accepting an HTTPX request and returning a 401 or the supplied success
        response.
    """
    def api(request):
        """Reject the configured expired token and validate that retried requests use the replacement token.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX 401 Response for the old token or the delegated successful response for the
            refreshed token.
        """
        if request.headers.get(header) == prefix + bad:
            return httpx.Response(401, json={'errors': [{'message': 'Invalid access token'}]})
        assert request.headers[header] == prefix + 'token-2'
        return good_response(request)
    return api


def test_orders_401_refreshes_once_and_retries_with_new_bearer_token(monkeypatch):
    """Regression check: orders 401 refreshes once and retries with new bearer token.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(reject_token('token-1', orders_ok))
    api = server.client()
    rows = api.orders('start', 'end')
    assert [r['quantity'] for r in rows] == [3]
    assert len(server.token_calls) == 2
    assert all(b'grant_type=refresh_token' in r.content for r in server.token_calls)
    assert [r.headers['Authorization'] for r in server.api_calls] == ['Bearer token-1', 'Bearer token-2']
    assert api.token == 'token-2'
    # The refreshed token is cached for later calls: no further refreshes.
    api.raw_orders('start', 'end')
    assert len(server.token_calls) == 2


def test_listings_401_refreshes_once_and_retries_with_new_iaf_token(monkeypatch):
    """Regression check: listings 401 refreshes once and retries with new iaf token.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    def listing(request):
        """Choose the active or unsold Trading API response based on the intercepted request XML.

        Args:
            request: HTTPX request intercepted by a MockTransport; no live service is contacted.

        Returns:
            HTTPX 200 Response containing the corresponding synthetic listing XML.
        """
        section = 'ActiveList' if b'<ActiveList>' in request.content else 'UnsoldList'
        return httpx.Response(200, text=xml(section, item='1' if section == 'ActiveList' else '2'))
    server = TokenServer(reject_token('token-1', listing, 'X-EBAY-API-IAF-TOKEN', ''))
    api = server.client()
    rows = api.listings('2026-09-23')
    assert len(rows) == 2
    assert len(server.token_calls) == 2
    tokens = [r.headers['X-EBAY-API-IAF-TOKEN'] for r in server.api_calls]
    assert tokens == ['token-1', 'token-2', 'token-2']
    # The retried request is otherwise identical (same call name and XML body).
    assert server.api_calls[0].content == server.api_calls[1].content
    assert server.api_calls[1].headers['X-EBAY-API-CALL-NAME'] == 'GetMyeBaySelling'


def test_category_401_refreshes_app_token_once(monkeypatch):
    """Regression check: category 401 refreshes app token once.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(reject_token('token-1', lambda r: httpx.Response(200, json={'categoryPath': 'Motors|Parts'})))
    api = server.client()
    assert api.category('123') == 'Motors > Parts'
    assert len(server.token_calls) == 2
    assert all(b'grant_type=client_credentials' in r.content for r in server.token_calls)
    assert api.app_token_value == 'token-2'
    assert api.token is None  # the seller token was never touched


@pytest.mark.parametrize('call', [lambda api: api.raw_orders('start', 'end'),
                                  lambda api: api.listings('2026-09-23'),
                                  lambda api: api.category('123')])
def test_second_consecutive_401_raises_without_looping(monkeypatch, call):
    """Regression check: second consecutive 401 raises without looping.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        call: Parameterized eBay operation used to exercise repeated authorization failure.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(lambda request: httpx.Response(401, text='denied'))
    api = server.client()
    with pytest.raises(EbayError, match='401'):
        call(api)
    assert len(server.api_calls) == 2
    assert len(server.token_calls) == 2


def test_401_from_refresh_token_endpoint_does_not_recurse(monkeypatch):
    """Regression check: 401 from refresh token endpoint does not recurse.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(orders_ok, token_status=401)
    api = server.client()
    with pytest.raises(EbayError, match='401'):
        api.raw_orders('start', 'end')
    assert len(server.token_calls) == 1
    assert server.api_calls == []
    with pytest.raises(EbayError, match='401'):
        api.app_token()
    assert len(server.token_calls) == 2


def test_api_401_then_token_endpoint_401_raises(monkeypatch):
    """Regression check: API 401 then token endpoint 401 raises.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(lambda request: httpx.Response(401, text='denied'))
    api = server.client()
    api.access_token()
    server.token_status = 401  # the refresh token itself has been revoked
    with pytest.raises(EbayError, match='401'):
        api.raw_orders('start', 'end')
    assert len(server.api_calls) == 1
    assert len(server.token_calls) == 2


@pytest.mark.parametrize('headers', [None, {'Content-Type': 'application/json'},
                                     {'Authorization': 'Bearer someone-elses-token'}])
def test_401_without_our_token_raises_without_refresh(monkeypatch, headers):
    """Regression check: 401 without our token raises without refresh.

    Args:
        monkeypatch: Pytest patch manager for temporary environment, clock, client, or
            dependency overrides.
        headers: HTTP headers supplied to exercise unauthorized requests without managed tokens.

    Returns:
        None. Successful completion means the assertions passed; unexpected results raise
        AssertionError or fail a pytest exception check.
    """
    refresh_env(monkeypatch)
    wall_clock(monkeypatch)
    server = TokenServer(lambda request: httpx.Response(401, text='denied'))
    api = server.client()
    kwargs = {'headers': headers} if headers is not None else {}
    with pytest.raises(EbayError, match='401'):
        api._request('GET', api.base + '/sell/fulfillment/v1/order', **kwargs)
    assert len(server.api_calls) == 1
    assert server.token_calls == []
    # allow_error callers get the 401 response back, still without a refresh.
    response = api._request('GET', api.base + '/sell/fulfillment/v1/order', allow_error=True, **kwargs)
    assert response.status_code == 401
    assert len(server.api_calls) == 2
    assert server.token_calls == []
