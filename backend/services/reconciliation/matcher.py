"""Attach Amazon order-confirmation emails to eBay orders.

- Auto MATCHED only when the best candidate scores >= auto_match_score, leads this order's
  runner-up by >= auto_match_margin, and no other unresolved order (PENDING, NEEDS_REVIEW or
  never evaluated; in this run or not) wants the same email about as much.
- Otherwise NEEDS_REVIEW (a plausible candidate exists) or PENDING (none yet).
- MATCHED and MANUAL rows are never changed automatically, including rows the user settles while
  a run is in progress.
- PENDING/NEEDS_REVIEW orders are re-evaluated each cycle until recheck_hours after the order,
  and after that whenever an open email in their window is fetched after their last evaluation
  (the history backfill or a Gmail outage can deliver emails long after the order was first
  evaluated). Orders never evaluated are evaluated once regardless of age.
- One email can back only one order (UNIQUE gmail_message_id), and emails a user unlinked
  from an order are never offered to it again.
"""
import json
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import timedelta

from .scoring import parse_time, score

CANDIDATES_KEPT = 5
UNRESOLVED = "(m.order_id IS NULL OR m.status IN ('PENDING','NEEDS_REVIEW'))"
# Order timestamps are compared as text in SQL only to narrow a query (eBay's carry milliseconds,
# which sort oddly against whole-second bounds); exact checks use parsed datetimes in Python.
SQL_SLACK = timedelta(minutes=1)
ORDER_FACTS = '''SELECT o.order_id, o.created_at, o.ship_first_name AS first_name, o.city, o.state,
        (SELECT sum(quantity) FROM order_items i WHERE i.order_id=o.order_id) AS quantity,
        (SELECT nullif(l.category,'') FROM order_items i JOIN listings l ON l.item_id=i.item_id
         WHERE i.order_id=o.order_id LIMIT 1) AS category,
        m.rejected_json FROM orders o LEFT JOIN order_matches m ON m.order_id=o.order_id'''


def now_iso(now):
    return now.isoformat(timespec='seconds').replace('+00:00', 'Z')


def order_facts(db, order_ids):
    if not order_ids:
        return []
    marks = ','.join('?' * len(order_ids))
    return [dict(r) for r in db.execute(f'{ORDER_FACTS} WHERE o.order_id IN ({marks})', order_ids)]


def open_emails(db, start, end):
    """Parsed confirmation emails received in [start, end] not attached to any order."""
    return [dict(r) for r in db.execute('''SELECT gmail_message_id, received_at, fetched_at, first_name, city,
            state, quantity, category, grand_total_cents FROM amazon_email_purchases e
            WHERE parse_status='parsed' AND received_at BETWEEN ? AND ?
            AND NOT EXISTS (SELECT 1 FROM order_matches m WHERE m.gmail_message_id=e.gmail_message_id)
            ORDER BY received_at, gmail_message_id''', (start, end))]


class EmailsByTime:
    """Emails sorted by received time, so each order only looks at the emails in its window."""

    def __init__(self, emails):
        self.emails = sorted(emails, key=lambda e: parse_time(e['received_at']))
        self.times = [parse_time(e['received_at']) for e in self.emails]

    def near(self, order_time, cfg):
        """Exactly the emails scoring.in_window accepts for an order placed at order_time."""
        return self.emails[bisect_left(self.times, order_time - timedelta(minutes=cfg.skew_minutes)):
                           bisect_right(self.times, order_time + timedelta(hours=cfg.window_hours))]


def orders_to_evaluate(db, cfg, now, start_date):
    """Never-evaluated orders, PENDING/NEEDS_REVIEW orders within recheck_hours, and older
    PENDING/NEEDS_REVIEW orders that got a new email since their last evaluation."""
    recent = now - timedelta(hours=cfg.recheck_hours)
    due, stale = [], []
    for r in db.execute(f'''SELECT o.order_id, o.created_at, m.order_id AS evaluated, m.updated_at FROM orders o
            LEFT JOIN order_matches m ON m.order_id=o.order_id WHERE o.created_at >= ? AND {UNRESOLVED}''',
                        (start_date,)):
        created = parse_time(r['created_at'])
        if r['evaluated'] is None or created >= recent:
            due.append(r['order_id'])
        else:
            stale.append((r['order_id'], created, parse_time(r['updated_at'])))
    return due + emailed_since_evaluation(db, cfg, stale)


def emailed_since_evaluation(db, cfg, stale):
    """Of (order_id, created, evaluated) tuples, the orders with an open email in their window that was
    fetched strictly after `evaluated`. The sync cycle stamps both with the same `now`, and an email
    fetched in the cycle that evaluated the order was already considered by that evaluation."""
    if not stale:
        return []
    emails = EmailsByTime(open_emails(db, now_iso(min(s[1] for s in stale) - timedelta(minutes=cfg.skew_minutes)),
                                      now_iso(max(s[1] for s in stale) + timedelta(hours=cfg.window_hours))))
    return [order_id for order_id, created, evaluated in stale
            if any(parse_time(e['fetched_at']) > evaluated for e in emails.near(created, cfg))]


def ranked_candidates(order, emails, cfg):
    """Eligible emails for an order, best first. Emails the user unlinked from it are never offered."""
    rejected = set(json.loads(order['rejected_json'] or '[]'))
    results = []
    for email in emails.near(parse_time(order['created_at']), cfg):
        if email['gmail_message_id'] in rejected:
            continue
        result = score(order, email, cfg)
        if result['eligible']:
            results.append({'gmail_message_id': email['gmail_message_id'], 'received_at': email['received_at'],
                            'grand_total_cents': email['grand_total_cents'], **result})
    return sorted(results, key=lambda r: -r['score'])


def outside_claims(db, cfg, scored, emails):
    """{gmail_message_id: [(score, order_id)]}: how much each unresolved order outside this run wants
    the emails this run might auto-match. Scored for the competition check only, never saved."""
    wanted = {r[0]['gmail_message_id'] for r in scored.values() if r and r[0]['score'] >= cfg.auto_match_score}
    contested = EmailsByTime([e for e in emails.emails if e['gmail_message_id'] in wanted])
    if not contested.emails:
        return {}
    rows = db.execute(f'{ORDER_FACTS} WHERE o.created_at BETWEEN ? AND ? AND {UNRESOLVED}',
                      (now_iso(contested.times[0] - timedelta(hours=cfg.window_hours) - SQL_SLACK),
                       now_iso(contested.times[-1] + timedelta(minutes=cfg.skew_minutes) + SQL_SLACK)))
    claims = {}
    for order in map(dict, rows):
        if order['order_id'] not in scored:
            for result in ranked_candidates(order, contested, cfg):
                claims.setdefault(result['gmail_message_id'], []).append((result['score'], order['order_id']))
    return claims


def run_matching(db, cfg, now, start_date='2000-01-01', order_ids=None):
    """Evaluate orders (default: those due for (re)evaluation). Caller owns the transaction.
    Returns counts by resulting status (orders whose result could not be saved are not counted)."""
    if order_ids is None:
        order_ids = orders_to_evaluate(db, cfg, now, start_date)
    else:  # Manual recheck: never touch confirmed matches.
        locked = {r['order_id'] for r in db.execute(
            f'''SELECT order_id FROM order_matches WHERE status IN ('MATCHED','MANUAL')
                AND order_id IN ({",".join("?" * len(order_ids))})''', order_ids)} if order_ids else set()
        order_ids = [o for o in order_ids if o not in locked]
    orders = order_facts(db, order_ids)
    if not orders:
        return {}
    times = [parse_time(o['created_at']) for o in orders]
    emails = EmailsByTime(open_emails(db, now_iso(min(times) - timedelta(minutes=cfg.skew_minutes)),
                                      now_iso(max(times) + timedelta(hours=cfg.window_hours))))
    scored = {order['order_id']: ranked_candidates(order, emails, cfg) for order in orders}
    # Every unresolved order that wants an email competes for it, whether or not it is in this run.
    claims = {}
    for order_id, results in scored.items():
        for result in results:
            claims.setdefault(result['gmail_message_id'], []).append((result['score'], order_id))
    for email_id, rivals in outside_claims(db, cfg, scored, emails).items():
        claims.setdefault(email_id, []).extend(rivals)
    # Strongest claims first, so a contested email goes to review rather than the first order.
    claimed, counts = set(), {}
    for order_id in sorted(scored, key=lambda o: -(scored[o][0]['score'] if scored[o] else 0)):
        results = scored[order_id]
        top = results[0] if results else None
        runner_up = results[1]['score'] if len(results) > 1 else 0
        rivals = [s for s, other in claims.get(top['gmail_message_id'], []) if other != order_id] if top else []
        decisive = (top and top['score'] >= cfg.auto_match_score and top['score'] - runner_up >= cfg.auto_match_margin
                    and all(top['score'] - s >= cfg.auto_match_margin for s in rivals)
                    and top['gmail_message_id'] not in claimed)
        if decisive:
            status, email_id, confidence = 'MATCHED', top['gmail_message_id'], top['score']
            claimed.add(email_id)
        else:
            status = 'NEEDS_REVIEW' if top and top['score'] >= cfg.review_score else 'PENDING'
            email_id, confidence = None, top['score'] if top else None
        if save_evaluation(db, order_id, status, email_id, confidence, results[:CANDIDATES_KEPT], now):
            counts[status] = counts.get(status, 0) + 1
    return counts


def save_evaluation(db, order_id, status, email_id, confidence, candidates, now):
    """Write an automatic result; returns whether it was written. Not written (the order is left for a
    later run) when the user made the row MATCHED/MANUAL after the matcher read it, or when the email
    being auto-claimed was meanwhile linked to another order (UNIQUE gmail_message_id)."""
    stamp = now_iso(now)
    try:
        written = db.execute('''INSERT INTO order_matches(order_id, status, gmail_message_id, confidence,
                      candidates_json, method, matched_at, updated_at) VALUES (?,?,?,?,?,?,?,?)
                      ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,
                      gmail_message_id=excluded.gmail_message_id, confidence=excluded.confidence,
                      candidates_json=excluded.candidates_json, method=excluded.method,
                      matched_at=excluded.matched_at, updated_at=excluded.updated_at
                      WHERE order_matches.status NOT IN ('MATCHED','MANUAL')''',
                             (order_id, status, email_id, confidence, json.dumps(candidates), 'auto',
                              stamp if email_id else None, stamp)).rowcount
    except sqlite3.IntegrityError:
        if email_id is None:
            raise
        return False  # SQLite undoes just this statement; the rest of the run proceeds.
    return written > 0
