"""Transparent eBay-order <-> Amazon-confirmation scoring. Every point is attributed to a named
signal so the UI can show exactly why a match was (or was not) made. Weights live in
config.json -> matching."""
import re
import unicodedata
from datetime import datetime, timedelta

US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR', 'california': 'CA', 'colorado': 'CO',
    'connecticut': 'CT', 'delaware': 'DE', 'district of columbia': 'DC', 'florida': 'FL', 'georgia': 'GA',
    'hawaii': 'HI', 'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD', 'massachusetts': 'MA',
    'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO', 'montana': 'MT',
    'nebraska': 'NE', 'nevada': 'NV', 'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC', 'south dakota': 'SD',
    'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY', 'puerto rico': 'PR', 'guam': 'GU',
    'virgin islands': 'VI', 'armed forces americas': 'AA', 'armed forces europe': 'AE', 'armed forces pacific': 'AP'}
CITY_PREFIXES = {'st': 'saint', 'ste': 'sainte', 'ft': 'fort', 'mt': 'mount', 'pt': 'port'}


def clean(text):
    """Lowercase ASCII words: accents folded ("José" == "Jose"), punctuation removed."""
    folded = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', re.sub(r"[^a-z0-9 ]+", ' ', folded.lower())).strip()


def norm_first_name(name):
    words = clean(name).split()
    return words[0] if words else None


def norm_city(city):
    words = clean(city).split()
    if words and words[0] in CITY_PREFIXES:
        words[0] = CITY_PREFIXES[words[0]]
    return ' '.join(words) or None


def norm_state(state):
    text = clean(state)
    if not text:
        return None
    if len(text) == 2:
        return text.upper()
    return US_STATES.get(text, text.upper())


def parse_time(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def in_window(order_time, email_time, cfg):
    return (order_time - timedelta(minutes=cfg.skew_minutes) <= email_time
            <= order_time + timedelta(hours=cfg.window_hours))


def score(order, email, cfg):
    """order: first_name, city, state, quantity, created_at, category (optional).
    email: first_name, city, state, quantity, received_at, category (optional).
    Returns {'eligible', 'score', 'minutes_after', 'signals': {name: {'points', 'detail'}}}."""
    order_time, email_time = parse_time(order['created_at']), parse_time(email['received_at'])
    minutes = round((email_time - order_time).total_seconds() / 60, 1)
    signals = {}

    def signal(name, points, detail):
        signals[name] = {'points': points, 'detail': detail}

    if not in_window(order_time, email_time, cfg):
        return {'eligible': False, 'score': 0, 'minutes_after': minutes, 'signals': signals,
                'reason': f'outside the -{cfg.skew_minutes:g} min / +{cfg.window_hours:g} h window'}
    order_state, email_state = norm_state(order.get('state')), norm_state(email.get('state'))
    if order_state and email_state and order_state != email_state:
        return {'eligible': False, 'score': 0, 'minutes_after': minutes, 'signals': signals,
                'reason': f'state {email_state} != {order_state}'}
    if order_state and email_state:
        signal('state', cfg.state_match, f'{email_state} matches')
    else:
        signal('state', 0, 'state missing')
    if order.get('quantity') and email.get('quantity'):
        same = int(order['quantity']) == int(email['quantity'])
        signal('quantity', cfg.quantity_match if same else cfg.quantity_mismatch,
               f'{email["quantity"]} vs {order["quantity"]}')
    else:
        signal('quantity', 0, 'quantity missing')
    for name, normalizer, hit, miss in (('first_name', norm_first_name, cfg.first_name_match, cfg.first_name_mismatch),
                                         ('city', norm_city, cfg.city_match, cfg.city_mismatch)):
        a, b = normalizer(order.get(name)), normalizer(email.get(name))
        if a and b:
            signal(name, hit if a == b else miss, f'"{b}" vs "{a}"')
        else:
            signal(name, 0, f'{name} missing')
    elapsed = max(0.0, minutes)
    fraction = max(0.0, 1 - elapsed / (cfg.window_hours * 60))
    signal('time', round(cfg.time_max * fraction, 1), f'{minutes:g} min after the eBay order')
    categories = set(clean(order.get('category')).split()) & set(clean(email.get('category')).split())
    if order.get('category') and email.get('category'):
        signal('category', cfg.category_match if categories else 0,
               'shared words: ' + ', '.join(sorted(categories)) if categories else 'no shared words')
    total = round(max(0.0, min(100.0, sum(s['points'] for s in signals.values()))), 1)
    return {'eligible': True, 'score': total, 'minutes_after': minutes, 'signals': signals}
