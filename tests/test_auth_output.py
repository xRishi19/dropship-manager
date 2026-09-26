import csv
import io
from urllib.parse import parse_qs, urlparse

import pytest

from ebay_auth import authorization_url, callback_code
from backend.services.sourcing.output import csv_text


def test_oauth_consent_scopes_and_state():
    url = authorization_url('client', 'ru-name', 'random-state')
    query = parse_qs(urlparse(url).query)
    assert query['state'] == ['random-state']
    assert query['redirect_uri'] == ['ru-name']
    assert 'sell.fulfillment.readonly' in query['scope'][0]
    assert callback_code('https://example.test/?code=abc%2Bdef&state=random-state', 'random-state') == 'abc+def'
    with pytest.raises(ValueError, match='state'):
        callback_code('https://example.test/?code=x&state=attacker', 'random-state')


def test_spreadsheet_formula_escaping_preserves_numeric_units():
    output = csv_text([{'title': '=HYPERLINK("bad")', 'units': 20}], ['title', 'units'])
    row = list(csv.DictReader(io.StringIO(output)))[0]
    assert row['title'].startswith("'=")
    assert row['units'] == '20'
