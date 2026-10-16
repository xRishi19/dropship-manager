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
    """Build the seller consent URL with the required scopes and anti-forgery state.

    Args:
        client_id: eBay OAuth application client ID.
        runame: Registered eBay redirect URI name (RuName).
        state: Random state value to verify at callback.
        sandbox: Whether to use the sandbox consent host instead of production.

    Returns:
        Encoded authorization URL string; performs no network requests.
    """
    host = 'https://auth.sandbox.ebay.com' if sandbox else 'https://auth.ebay.com'
    return host + '/oauth2/authorize?' + urlencode({'client_id': client_id,
        'redirect_uri': runame, 'response_type': 'code', 'scope': SCOPES, 'state': state})


def callback_code(url, expected_state):
    """Extract a single authorization code from a redirect URL after constant-time OAuth state validation.

    Args:
        url: Complete browser redirect URL containing code and state query parameters.
        expected_state: State originally generated for this consent attempt.

    Returns:
        Authorization code string. Raises ValueError for state mismatch or a missing/ambiguous code.
    """
    query = parse_qs(urlparse(url).query)
    if not secrets.compare_digest(query.get('state', [''])[0], expected_state):
        raise ValueError('OAuth state mismatch. Restart setup and use the complete redirect URL.')
    if len(query.get('code', [])) != 1:
        raise ValueError('Authorization code missing. Consent may have been declined.')
    return query['code'][0]


def main():
    """Run interactive seller OAuth setup, exchange the hidden callback URL for tokens, and securely save the refresh token in dotenv.

    Args:
        None.

    Returns:
        Exit code 0 on success or 1 for missing credentials or handled setup failure. Reads process arguments, contacts eBay, writes the env file, and always closes the API client.
    """
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
