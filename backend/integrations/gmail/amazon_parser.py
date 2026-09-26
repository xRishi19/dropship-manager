"""Parse Amazon order-confirmation emails into the few fields used for reconciliation.

Amazon Order IDs are deliberately ignored. The Gmail message ID is the unique key.
Calibrated on the seller's real 2026 confirmations (an anonymized copy of the layout is
in tests/test_gmail_and_cycle.py):

    Subject: Ordered 2 items: Automotive
    ...
    Alex, thanks for your order!         <- the Amazon account holder, NOT the recipient
    Arriving Monday
    2 Automotive items
    Jordan - SPOKANE, WA                 <- recipient first name - CITY, ST
    Order # ...                          <- ignored
    Grand Total:
     $71.50

Older "Ship to: ..." and 'Ordered: "Product" and N more items' layouts are still recognized.

parse_status:
- parsed:  Grand Total plus at least one recipient field; eligible for automatic matching
- partial: Grand Total only; can be linked to an order manually
- failed:  no Grand Total found
"""
import html
import re
from decimal import Decimal
from html.parser import HTMLParser

MONEY = r'\$?\s*(?:USD\s*)?([\d,]+\.\d{2})'
TOTAL_PATTERNS = [rf'Grand\s+Total\s*:?\s*{MONEY}', rf'Order\s+Total\s*:?\s*{MONEY}', rf'\bTotal\s*:?\s*{MONEY}']
RECIPIENT_PATTERNS = [
    # Current layout: a line of its own, "Jordan - SPOKANE, WA" (optionally followed by a ZIP).
    # Letters include accents ("Renée"): [^\W\d_] is any Unicode letter.
    r'^(?P<name>[^\W\d_](?:[^\W\d_]|[.\'-])*(?: [^\W\d_](?:[^\W\d_]|[.\'-])*){0,3})\s+[-–—]\s+'
    r'(?P<city>[^\W\d_](?:[^\W\d_]|[ .\'-])*?)\s*,\s*(?P<state>[A-Za-z]{2})\s*(?:\d{5}(?:-\d{4})?)?\s*$',
    # "Ship to: John Smith - SPRINGFIELD, IL" / "Deliver to John\nSpringfield, Illinois 62704"
    r'(?:Ship(?:ping)?\s+to|Deliver(?:ing|y)?\s+to|(?:will\s+be\s+)?sent\s+to)\s*:?\s*'
    r'(?P<name>[A-Za-z][A-Za-z .\'-]*?)\s*(?:[-–—,]|\n)\s*'
    r'(?P<city>[A-Za-z][A-Za-z .\'-]*?)\s*,\s*(?P<state>[A-Za-z][A-Za-z .]*?)(?:\s+\d{5}(?:-\d{4})?)?\s*(?:\n|$|,|\s{2,})',
]
QUANTITY_PATTERNS = [r'\bQuantity\s*:?\s*(\d+)', r'\bQty\.?\s*:?\s*(\d+)']
# "Ordered 1 item: Supplements" / "Ordered 2 items: Automotive" / "Ordered: 2 Health Care items"
SUBJECT_COUNT = re.compile(r'Ordered:?\s+(?P<count>\d+)\s+(?:(?P<before>.+?)\s+)?items?\b(?:\s*:\s*(?P<after>.+))?', re.I)
# Body line "2 Automotive items" (fallback when the subject has no count).
BODY_COUNT = re.compile(r'^\s*(?P<count>\d+)\s+(?:.+?\s+)?items?\b', re.I | re.M)
SUBJECT_ITEM = re.compile(r'Ordered:\s*["“](?P<product>.+?)["”]'
                          r'(?:\s+and\s+(?P<more>\d+)\s+more\s+items?)?', re.I)
# Invisible preheader padding and text-direction marks Amazon inserts around text.
INVISIBLE = re.compile('[͏­​-‏‪-‮⁠⁦-⁩﻿]')
AMAZON_ORDER_NUMBER = re.compile(r'\b\d{3}-\d{7}-\d{7}\b')


class _Text(HTMLParser):
    BLOCK = {'p', 'div', 'br', 'tr', 'td', 'li', 'table', 'h1', 'h2', 'h3', 'h4', 'span'}
    HIDDEN = {'style', 'script', 'head', 'title'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden += 1
        if tag in self.BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def to_text(body, is_html):
    if is_html:
        parser = _Text()
        parser.feed(body)
        body = ''.join(parser.parts)
    text = INVISIBLE.sub('', html.unescape(body)).replace('\r', '').replace('\xa0', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n', text).strip()


def redact(body):
    """Amazon Order IDs are never stored or used."""
    return AMAZON_ORDER_NUMBER.sub('[order number removed]', body or '')


def cents(value):
    return int(Decimal(value.replace(',', '')) * 100)


def parse(subject, body, is_html=True):
    text = to_text(body or '', is_html)
    result = {'first_name': None, 'city': None, 'state': None, 'quantity': None, 'category': None,
              'grand_total_cents': None, 'parse_status': 'failed', 'parse_error': None}
    for pattern in TOTAL_PATTERNS:
        found = re.search(pattern, text, re.I)
        if found:
            result['grand_total_cents'] = cents(found.group(1))
            break
    for pattern in RECIPIENT_PATTERNS:
        found = re.search(pattern, text, re.I | re.M)
        if found:
            name = found.group('name').split()
            result.update(first_name=name[0].title() if name else None,
                          city=found.group('city').strip().title(), state=found.group('state').strip())
            break
    quantities = [int(q) for p in QUANTITY_PATTERNS for q in re.findall(p, text, re.I)]
    subject = INVISIBLE.sub('', subject or '')
    subject_count = SUBJECT_COUNT.search(subject)
    subject_match = SUBJECT_ITEM.search(subject)
    body_count = BODY_COUNT.search(text)
    if quantities:
        result['quantity'] = sum(quantities)
    elif subject_count:
        result['quantity'] = int(subject_count.group('count'))
    elif subject_match:
        result['quantity'] = 1 + int(subject_match.group('more') or 0)
    elif body_count:
        result['quantity'] = int(body_count.group('count'))
    category = subject_count and (subject_count.group('after') or subject_count.group('before'))
    if category:
        result['category'] = category.strip()[:120]
    elif subject_match:
        result['category'] = subject_match.group('product')[:120]
    if result['grand_total_cents'] is None:
        result['parse_error'] = 'Grand Total not found'
    elif result['first_name'] or result['city'] or result['state']:
        result['parse_status'] = 'parsed'
    else:
        result['parse_status'] = 'partial'
        result['parse_error'] = 'Recipient name/city/state not found'
    return result
