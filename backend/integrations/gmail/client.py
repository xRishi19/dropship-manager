"""Read-only Gmail API access (OAuth desktop flow; token stored locally, never committed)."""
import base64
from pathlib import Path

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']


class GmailNotConfigured(RuntimeError):
    pass


def load_credentials(token_path):
    """Load saved read-only Gmail credentials, refresh expired tokens when possible, and persist refreshed tokens with owner-only permissions.

    Args:
        token_path: Local authorized-user JSON token filename.

    Returns:
        Google Credentials instance. Raises GmailNotConfigured for absent or unusable credentials; refresh/filesystem failures propagate.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    path = Path(token_path)
    if not path.exists():
        raise GmailNotConfigured('Gmail is not connected; run: python -m backend.integrations.gmail.auth')
    credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not credentials.valid:
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            path.write_text(credentials.to_json())
            path.chmod(0o600)
        else:
            raise GmailNotConfigured('Gmail authorization expired; run: python -m backend.integrations.gmail.auth')
    return credentials


class GmailClient:
    def __init__(self, token_path, service=None):
        """Attach an injected Gmail service or build the Gmail v1 client using local read-only OAuth credentials.

        Args:
            token_path: Authorized-user token filename used when no service is supplied.
            service: Optional injected Gmail API service, such as a test double.

        Returns:
            None. Stores the service; default construction may refresh and persist credentials.
        """
        if service is None:
            from googleapiclient.discovery import build
            service = build('gmail', 'v1', credentials=load_credentials(token_path), cache_discovery=False)
        self.service = service

    def search(self, query):
        """Collect every message ID matching a Gmail search query across all result pages.

        Args:
            query: Gmail search syntax sent as the q parameter for the current user.

        Returns:
            List of message ID strings; API failures propagate to the caller.
        """
        ids, token = [], None
        while True:
            response = self.service.users().messages().list(userId='me', q=query, pageToken=token,
                                                             maxResults=500).execute()
            ids += [m['id'] for m in response.get('messages', [])]
            token = response.get('nextPageToken')
            if not token:
                return ids

    def get(self, message_id):
        """Fetch a full Gmail message and decode its MIME parts, preferring the first HTML body over plain text.

        Args:
            message_id: Gmail message ID for the current user.

        Returns:
            Dictionary containing id, internal_date_ms, subject, body, and is_html; undecodable UTF-8 bytes use replacement characters.
        """
        message = self.service.users().messages().get(userId='me', id=message_id, format='full').execute()
        headers = {h['name'].lower(): h['value'] for h in message.get('payload', {}).get('headers', [])}
        html_body, text_body = None, None
        stack = [message.get('payload', {})]
        while stack:
            part = stack.pop()
            stack += part.get('parts', [])
            data = (part.get('body') or {}).get('data')
            if not data:
                continue
            decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', 'replace')
            if part.get('mimeType') == 'text/html' and html_body is None:
                html_body = decoded
            elif part.get('mimeType') == 'text/plain' and text_body is None:
                text_body = decoded
        return {'id': message['id'], 'internal_date_ms': int(message.get('internalDate', 0)),
                'subject': headers.get('subject', ''), 'body': html_body or text_body or '',
                'is_html': html_body is not None}
