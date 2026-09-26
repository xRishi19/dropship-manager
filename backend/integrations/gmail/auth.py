"""One-time Gmail consent: python -m backend.integrations.gmail.auth

Needs a Google Cloud OAuth *Desktop app* client saved at gmail.client_secret_path
(default secrets/gmail_client.json). Opens a browser for read-only consent and stores the
token at gmail.token_path (default data/gmail_token.json). Neither file is committed.
"""
import argparse
import sys
from pathlib import Path

from ...config import load_config
from .client import SCOPES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='config.json')
    args = parser.parse_args(argv)
    config = load_config(args.config).gmail
    secret = Path(config.client_secret_path)
    if not secret.exists():
        print(f'Save your Google OAuth desktop client JSON to {secret} first (see docs/setup.md).', file=sys.stderr)
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow
    credentials = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    token = Path(config.token_path)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(credentials.to_json())
    token.chmod(0o600)
    print(f'Gmail connected (read-only). Token saved to {token}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
