#!/usr/bin/env python3
"""One-time seller OAuth setup. Never prints tokens or takes secrets in arguments."""
import argparse
import getpass
import os
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlparse

from dotenv import load_dotenv, set_key

from backend.config import EbayConfig
from backend.integrations.ebay.client import EbayClient, SCOPES


def authorization_url(client_id, runame, state, sandbox=False):
    host = 'https://auth.sandbox.ebay.com' if sandbox else 'https://auth.ebay.com'
    return host + '/oauth2/authorize?' + urlencode({'client_id': client_id,
        'redirect_uri': runame, 'response_type': 'code', 'scope': SCOPES, 'state': state})


def callback_code(url, expected_state):
    query = parse_qs(urlparse(url).query)
    if not secrets.compare_digest(query.get('state', [''])[0], expected_state):
        raise ValueError('OAuth state mismatch. Restart setup and use the complete redirect URL.')
    if len(query.get('code', [])) != 1:
        raise ValueError('Authorization code missing. Consent may have been declined.')
    return query['code'][0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', default='.env')
    args = parser.parse_args()
    load_dotenv(args.env)
    required = ['EBAY_CLIENT_ID', 'EBAY_CLIENT_SECRET', 'EBAY_REDIRECT_URI']
    if any(not os.getenv(key) for key in required):
        print('Populate ' + ', '.join(required) + ' in .env first. EBAY_REDIRECT_URI is your eBay RuName.', file=sys.stderr)
        return 1
    state = secrets.token_urlsafe(32)
    print('Open this URL, sign into your seller account and grant access:')
    print(authorization_url(os.environ['EBAY_CLIENT_ID'], os.environ['EBAY_REDIRECT_URI'], state,
                            os.getenv('EBAY_ENVIRONMENT') == 'sandbox'))
    api = EbayClient(EbayConfig())
    try:
        url = getpass.getpass('Paste the full redirect URL here (hidden input): ')
        code = callback_code(url, state)
        response = api._request('POST', api.base + '/identity/v1/oauth2/token',
            auth=(os.environ['EBAY_CLIENT_ID'], os.environ['EBAY_CLIENT_SECRET']),
            data={'grant_type': 'authorization_code', 'code': code,
                  'redirect_uri': os.environ['EBAY_REDIRECT_URI']}).json()
        if not response.get('refresh_token'):
            raise ValueError('No refresh token returned; restart seller consent.')
        set_key(args.env, 'EBAY_REFRESH_TOKEN', response['refresh_token'])
        os.chmod(args.env, 0o600)
        print('Seller refresh token saved to .env. You can now run python main.py --import.')
        return 0
    except (ValueError, RuntimeError):
        print('OAuth setup failed. Check credentials, RuName, consent scopes and the complete redirect URL; then restart.', file=sys.stderr)
        return 1
    finally:
        api.close()


if __name__ == '__main__':
    sys.exit(main())
