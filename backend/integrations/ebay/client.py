"""Read-only eBay access: seller OAuth, Fulfillment orders, Finances transactions,
Post-Order returns, Trading listings and Browse categories. Shared by accounting and sourcing."""
import logging
import os
import time
import xml.etree.ElementTree as ET

import httpx

from ...db.ingest import normalize
from .mapping import order_sales_rows

LOG = logging.getLogger(__name__)
BASE_SCOPES = 'https://api.ebay.com/oauth/api_scope https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly'
# Finances (net payouts and fees) needs one more consented scope; run ebay_auth.py again.
SCOPES = BASE_SCOPES + ' https://api.ebay.com/oauth/api_scope/sell.finances'
NS = {'e': 'urn:ebay:apis:eBLBaseComponents'}
MARKETPLACE = 'EBAY_US'


class EbayError(RuntimeError):
    pass


class EbayClient:
    def __init__(self, config, client=None):
        self.config = config
        environment = os.getenv('EBAY_ENVIRONMENT', 'production')
        if environment not in {'production', 'sandbox'}:
            raise EbayError('EBAY_ENVIRONMENT must be production or sandbox')
        sandbox = environment == 'sandbox'
        self.base = 'https://api.sandbox.ebay.com' if sandbox else 'https://api.ebay.com'
        self.finances_base = 'https://apiz.sandbox.ebay.com' if sandbox else 'https://apiz.ebay.com'
        self.client = client or httpx.Client(timeout=60)
        self.token = None
        # Wall-clock expiry (time.time): time.monotonic() does not advance while the Mac sleeps.
        self.expires_at = 0
        self.app_token_value = None
        self.app_expires_at = 0
        # True when the stored refresh token predates the finances scope.
        self.finances_scope_missing = False

    def close(self):
        self.client.close()

    def _request(self, method, url, allow_error=False, _retried=False, **kwargs):
        for attempt in range(4):
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError:
                if attempt == 3:
                    raise EbayError('eBay network request failed after retries') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                delay = response.headers.get('Retry-After', '')
                time.sleep(min(float(delay), 30) if delay.isdigit() else 2 ** attempt)
                continue
            if response.status_code == 401 and not _retried:
                # The access token was rejected (e.g. it expired while the Mac slept, or eBay revoked
                # it early): get a fresh one from the long-lived refresh token and retry once.
                headers = self._with_fresh_token(kwargs.get('headers'))
                if headers is not None:
                    return self._request(method, url, allow_error, True, **{**kwargs, 'headers': headers})
            if response.is_error and not allow_error:
                raise EbayError(f'eBay HTTP {response.status_code}; check seller OAuth scopes/credentials and API access')
            return response
        raise EbayError('eBay request failed')

    def _with_fresh_token(self, headers):
        """Copy of request headers carrying a newly refreshed token, or None if no token was sent."""
        if not headers:
            return None
        for key in ('Authorization', 'X-EBAY-API-IAF-TOKEN'):
            value = headers.get(key)
            if not value:
                continue
            if self.token and value.endswith(self.token):
                old, new = self.token, self.access_token(force=True)
            elif self.app_token_value and value.endswith(self.app_token_value):
                old, new = self.app_token_value, self.app_token(force=True)
            else:
                continue
            return {**headers, key: value[:-len(old)] + new}
        return None

    def access_token(self, force=False):
        if not force and self.token and time.time() < self.expires_at:
            return self.token
        refresh = os.getenv('EBAY_REFRESH_TOKEN')
        if refresh:
            client_id, secret = os.getenv('EBAY_CLIENT_ID'), os.getenv('EBAY_CLIENT_SECRET')
            if not client_id or not secret:
                raise EbayError('Refresh requires EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env')
            response = self._refresh(client_id, secret, refresh, SCOPES)
            if response.status_code == 400 and 'invalid_scope' in response.text:
                # Consent predates the finances scope: keep syncing orders/listings meanwhile.
                self.finances_scope_missing = True
                response = self._refresh(client_id, secret, refresh, BASE_SCOPES)
            if response.is_error:
                raise EbayError(f'eBay HTTP {response.status_code}; check seller OAuth scopes/credentials and API access')
            response = response.json()
            self.token = response['access_token']
            self.expires_at = time.time() + max(0, int(response['expires_in']) - 60)
        else:
            self.token = os.getenv('EBAY_ACCESS_TOKEN')
            if not self.token:
                raise EbayError('Set eBay seller OAuth refresh credentials or EBAY_ACCESS_TOKEN in .env; an app-only key is insufficient')
            self.expires_at = time.time() + 300
        return self.token

    def _refresh(self, client_id, secret, refresh, scopes):
        return self._request('POST', self.base + '/identity/v1/oauth2/token', allow_error=True,
            auth=(client_id, secret), data={'grant_type': 'refresh_token', 'refresh_token': refresh, 'scope': scopes})

    def app_token(self, force=False):
        """Application token (client credentials) for public Browse data such as categories."""
        if not force and self.app_token_value and time.time() < self.app_expires_at:
            return self.app_token_value
        client_id, secret = os.getenv('EBAY_CLIENT_ID'), os.getenv('EBAY_CLIENT_SECRET')
        if not client_id or not secret:
            raise EbayError('Category lookup requires EBAY_CLIENT_ID and EBAY_CLIENT_SECRET in .env')
        response = self._request('POST', self.base + '/identity/v1/oauth2/token', auth=(client_id, secret),
            data={'grant_type': 'client_credentials', 'scope': 'https://api.ebay.com/oauth/api_scope'}).json()
        self.app_token_value = response['access_token']
        self.app_expires_at = time.time() + max(0, int(response['expires_in']) - 60)
        return self.app_token_value

    def raw_orders(self, start, end, date_filter='creationdate'):
        """Complete Fulfillment orders. Fixed endpoint + offset avoids forwarding
        credentials to response-provided URLs."""
        result, seen = [], set()
        offset = 0
        while True:
            payload = self._request('GET', self.base + '/sell/fulfillment/v1/order',
                headers={'Authorization': 'Bearer ' + self.access_token()},
                params={'filter': f'{date_filter}:[{start}..{end}]', 'limit': 200, 'offset': offset}).json()
            orders = payload.get('orders', [])
            for order in orders:
                order_id = order['orderId']
                if order_id in seen:
                    raise EbayError('Order pagination repeated an order; retry sync to avoid partial counts')
                seen.add(order_id)
                line_seen = set()
                for item in order.get('lineItems', []):
                    line_id = item['lineItemId']
                    if line_id in line_seen:
                        raise EbayError('Duplicate lineItemId in order response')
                    line_seen.add(line_id)
                    if not item.get('legacyItemId'):
                        raise EbayError('Order is missing legacyItemId; refusing unjoinable listing data')
                result.append(order)
            offset += len(orders)
            if offset >= int(payload.get('total', offset)) and not payload.get('next'):
                break
            if not orders:
                raise EbayError('Order pagination stopped before all results were downloaded')
            if offset >= 10000:
                raise EbayError('Order result limit reached; sync a shorter date range')
        return result

    def orders(self, start, end, date_filter='creationdate'):
        """Sourcing `sales` rows (one per line item; cancelled orders count 0 units)."""
        return [normalize(row, 'orders') for order in self.raw_orders(start, end, date_filter)
                for row in order_sales_rows(order)]

    def finance_transactions(self, start, end):
        """Finances getTransactions by transactionDate: sales (net of fees), ad fees, refunds."""
        result, offset = [], 0
        while True:
            payload = self._request('GET', self.finances_base + '/sell/finances/v1/transaction',
                headers={'Authorization': 'Bearer ' + self.access_token(), 'X-EBAY-C-MARKETPLACE-ID': MARKETPLACE},
                params={'filter': f'transactionDate:[{start}..{end}]', 'limit': 1000, 'offset': offset}).json()
            batch = payload.get('transactions', [])
            result += batch
            offset += len(batch)
            if not batch or offset >= int(payload.get('total', offset)):
                return result

    def returns(self, start, end):
        """Post-Order return search (page-numbered). VERIFY: seller OAuth access to Post-Order."""
        result, page = [], 1
        while True:
            payload = self._request('GET', self.base + '/post-order/v2/return/search',
                headers={'Authorization': 'IAF ' + self.access_token(), 'X-EBAY-C-MARKETPLACE-ID': MARKETPLACE},
                params={'creation_date_range_from': start, 'creation_date_range_to': end,
                        'limit': 200, 'offset': page}).json()
            result += payload.get('members', [])
            pages = int((payload.get('paginationOutput') or {}).get('totalPages') or 1)
            if page >= pages or not payload.get('members'):
                return result
            page += 1

    def category(self, item_id):
        """Browse category path for a listing ("Motors > Parts & Accessories > ..."), or None."""
        response = self._request('GET', self.base + '/buy/browse/v1/item/get_item_by_legacy_id', allow_error=True,
            headers={'Authorization': 'Bearer ' + self.app_token(), 'X-EBAY-C-MARKETPLACE-ID': MARKETPLACE},
            params={'legacy_item_id': item_id})
        if response.status_code == 404:
            return None  # Ended listings are not always available through Browse.
        if response.is_error:
            raise EbayError(f'eBay HTTP {response.status_code} looking up category')
        path = response.json().get('categoryPath')
        return path.replace('|', ' > ') if path else None

    def listings(self, observed_date):
        rows = {}
        for container, status in [('ActiveList', 'active'), ('UnsoldList', 'ended_unsold')]:
            page = 1
            downloaded = 0
            expected_total = None
            while True:
                duration = '<DurationInDays>60</DurationInDays>' if container == 'UnsoldList' else ''
                body = (f'<GetMyeBaySellingRequest xmlns="{NS["e"]}">'
                        '<HideVariations>false</HideVariations>'
                        f'<{container}><Include>true</Include>{duration}<Pagination>'
                        f'<EntriesPerPage>200</EntriesPerPage><PageNumber>{page}</PageNumber>'
                        f'</Pagination></{container}></GetMyeBaySellingRequest>')
                response = self._request('POST', self.base + '/ws/api.dll', content=body.encode(),
                    headers={'Content-Type': 'text/xml', 'X-EBAY-API-CALL-NAME': 'GetMyeBaySelling',
                    'X-EBAY-API-SITEID': str(self.config.site_id),
                    'X-EBAY-API-COMPATIBILITY-LEVEL': self.config.trading_version,
                    'X-EBAY-API-IAF-TOKEN': self.access_token()})
                root = ET.fromstring(response.content)
                if root.findtext('e:Ack', namespaces=NS) not in {'Success', 'Warning'}:
                    codes = [e.text for e in root.findall('e:Errors/e:ErrorCode', NS)]
                    raise EbayError(f'eBay Trading API failed (codes {codes}); check token scope/API access')
                if root.findtext('e:Ack', namespaces=NS) == 'Warning':
                    raise EbayError('eBay Trading API returned warnings; refusing potentially incomplete listing coverage')
                section = root.find(f'e:{container}', NS)
                if section is None:
                    raise EbayError(f'Missing {container} response; cannot establish unsold coverage')
                pages = int(section.findtext('e:PaginationResult/e:TotalNumberOfPages', default='0', namespaces=NS))
                total = int(section.findtext('e:PaginationResult/e:TotalNumberOfEntries', default='0', namespaces=NS))
                if expected_total is not None and total != expected_total:
                    raise EbayError('Listing count changed during pagination; retry sync')
                expected_total = total
                if total >= 25000:
                    raise EbayError('GetMyeBaySelling 25,000-listing cap reached; full coverage cannot be guaranteed')
                items = section.findall('e:ItemArray/e:Item', NS)
                downloaded += len(items)
                for item in items:
                    def value(path, default=''):
                        return item.findtext(path, default=default, namespaces=NS)
                    item_id = value('e:ItemID')
                    # Parent totals only: do not sum parent + variation counters.
                    row = normalize({'item_id': item_id, 'title': value('e:Title'),
                        'sku': value('e:SKU') if item.find('e:Variations', NS) is None else '',
                        'sold_quantity': value('e:SellingStatus/e:QuantitySold', '0'),
                        'observed_date': observed_date, 'status': status,
                        'start_date': value('e:ListingDetails/e:StartTime') or None,
                        # Active listings report a scheduled (future) end; only ended ones are real.
                        'end_date': (value('e:ListingDetails/e:EndTime') or None) if status == 'ended_unsold' else None},
                        'listings')
                    if item_id in rows:
                        raise EbayError('Listing moved/repeated during pagination; retry a consistent download')
                    rows[item_id] = row
                if page >= pages:
                    if downloaded != total:
                        raise EbayError('Listing download count does not match eBay pagination total')
                    break
                if not items:
                    raise EbayError('Empty listing page before pagination completed')
                page += 1
        return list(rows.values())
