"""One-time import of Amazon order-history CSVs (browser-extension export) to fill in Amazon
costs for older eBay orders whose confirmation emails lack a Grand Total.

    .venv/bin/python -m backend.tools.import_amazon_history secrets/*.csv            # dry run + report
    .venv/bin/python -m backend.tools.import_amazon_history secrets/*.csv --apply    # write (backs up DB)

Expected columns: order id, items, to, date, total, refund (one row per Amazon order).
- The Amazon order ID is used only to de-duplicate rows (hashed); it is never stored or shown.
- Orders to the seller's own names (config matching.personal_recipients) are skipped.
- Only eBay orders without an Amazon cost (PENDING / NEEDS_REVIEW / never evaluated), created
  on or before --until, are considered. MATCHED and MANUAL orders are never touched.

Matching, per eBay order, against Amazon orders placed from 1 day before to 4 days after it
(the export has dates, not times). eBay drops buyer names from orders older than ~90 days, so
product-title agreement is the main signal:
- title: share of the eBay title's words found in the Amazon item titles, up to 60 points
- date: +20 same day, +15 next day, +10 two days (or the day before), +5 three days, 0 four days
- name: +20 when the recipient's first name agrees; a different first name rules the order out
MATCH when title agreement >= 60% (>= 80% for 3-4 days later), the score leads this order's
runner-up by >= 15, no other eBay order wants the same Amazon order about as much, and the
titles don't start with different brands while agreeing under 70%.
REVIEW when a plausible candidate exists (>= 40% title agreement); otherwise NO_MATCH.
Cost = the Amazon order total (like the email Grand Total); refunds are reported, not deducted.
"""
import argparse
import csv
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from ..config import load_config
from ..db.connection import backup_database, connect
from ..services.financials.aggregation import local_zone
from ..services.reconciliation.scoring import clean, norm_first_name

SOURCE_PREFIX = 'amazon-csv:'
STOP = set('for and with the of to in on a an by from fits fit compatible replacement new pack pcs set kit'.split())
DAY_POINTS = {-1: 10, 0: 20, 1: 15, 2: 10, 3: 5, 4: 0}
REPORT_FIELDS = ['decision', 'ebay_order_id', 'ebay_date', 'ebay_title', 'confidence', 'amazon_date', 'amazon_to',
                 'amazon_items', 'amazon_total', 'amazon_refund', 'title_agreement', 'days_after', 'name',
                 'runner_up_score', 'note']


def words(text):
    return {w for w in clean(text).split() if len(w) > 1 and w not in STOP}


def money_cents(value):
    """Dollar text -> cents; None for blanks and non-numbers (e.g. a spreadsheet =SUBTOTAL row)."""
    value = (value or '').replace('$', '').replace(',', '').strip()
    try:
        return round(float(value) * 100) if value else None
    except ValueError:
        return None


def read_amazon(paths, personal):
    """Amazon orders from the CSVs, minus personal ones and rows without a date or total."""
    personal = {p.lower() for p in personal}
    orders, skipped = {}, {'personal': 0, 'no total or date': 0, 'duplicate': 0}
    for path in paths:
        with open(path, encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                first = (norm_first_name(row.get('to')) or '')
                if first in personal:
                    skipped['personal'] += 1
                    continue
                total = money_cents(row.get('total'))
                if total is None or not (row.get('date') or '').strip() or not (row.get('order id') or '').strip():
                    skipped['no total or date'] += 1
                    continue
                key = SOURCE_PREFIX + hashlib.sha256(row['order id'].strip().encode()).hexdigest()[:20]
                if key in orders:
                    skipped['duplicate'] += 1
                    continue
                orders[key] = {'id': key, 'date': date.fromisoformat(row['date'].strip()), 'to': row.get('to', '').strip(),
                               'items': row.get('items', '').strip().rstrip(';').strip(), 'total_cents': total,
                               'refund_cents': money_cents(row.get('refund')), 'words': words(row.get('items'))}
    return list(orders.values()), skipped


def open_ebay_orders(db, until):
    rows = db.execute('''SELECT o.order_id, o.created_at, o.ship_name, group_concat(i.title, ' | ') AS titles
        FROM orders o JOIN order_items i ON i.order_id=o.order_id
        LEFT JOIN order_matches m ON m.order_id=o.order_id
        WHERE (m.order_id IS NULL OR m.status IN ('PENDING','NEEDS_REVIEW')) AND o.created_at < ?
        GROUP BY o.order_id ORDER BY o.created_at''', (until,)).fetchall()
    return [dict(r) for r in rows]


def score(order, amazon, local_day):
    days = (amazon['date'] - local_day).days
    if days not in DAY_POINTS:
        return None
    ebay_words = words(order['titles'])
    agreement = len(ebay_words & amazon['words']) / len(ebay_words) if ebay_words else 0.0
    signals = {'title': {'points': round(60 * agreement, 1), 'detail': f'{agreement:.0%} of eBay title words'},
               'date': {'points': DAY_POINTS[days], 'detail': f'Amazon order {days:+d} day(s)'}}
    ebay_name, amazon_name = norm_first_name(order['ship_name']), norm_first_name(amazon['to'])
    if ebay_name and amazon_name:
        if ebay_name != amazon_name:
            return None  # Different recipient.
        signals['name'] = {'points': 20, 'detail': f'"{amazon_name}" matches'}
    total = round(sum(s['points'] for s in signals.values()), 1)
    return {'amazon': amazon, 'score': total, 'agreement': agreement, 'days': days, 'signals': signals,
            'name': 'match' if 'name' in signals else 'unknown'}


def plan(db, amazon_orders, until, zone):
    orders = open_ebay_orders(db, until)
    scored = {}
    for order in orders:
        local_day = datetime.fromisoformat(order['created_at'].replace('Z', '+00:00')).astimezone(zone).date()
        order['local_day'] = local_day
        results = [r for a in amazon_orders if (r := score(order, a, local_day))]
        scored[order['order_id']] = sorted(results, key=lambda r: (-r['score'], r['days']))
    wanted = {}
    for order_id, results in scored.items():
        for r in results:
            wanted.setdefault(r['amazon']['id'], []).append((r['score'], order_id))
    decisions, claimed = [], set()
    ranked = sorted(orders, key=lambda o: -(scored[o['order_id']][0]['score'] if scored[o['order_id']] else 0))
    for order in ranked:
        results = scored[order['order_id']]
        top = results[0] if results else None
        runner = results[1]['score'] if len(results) > 1 else 0
        note = ''
        decision = 'NO_MATCH'
        if top:
            rivals = [s for s, other in wanted[top['amazon']['id']] if other != order['order_id']]
            needed = 0.8 if top['days'] >= 3 else 0.6
            ebay_first = (clean(order['titles']).split() or [''])[0]
            amazon_first = (clean(top['amazon']['items']).split() or [''])[0]
            # A different leading brand word plus a weak title match: likely a different product.
            brand_conflict = (ebay_first != amazon_first and ebay_first not in top['amazon']['words']
                              and top['agreement'] < 0.7)
            if brand_conflict:
                note = f'brand differs ("{ebay_first}" vs "{amazon_first}")'
            if (top['agreement'] >= needed and top['score'] - runner >= 15 and top['amazon']['id'] not in claimed
                    and all(top['score'] - s >= 15 for s in rivals) and not brand_conflict):
                decision = 'MATCH'
                claimed.add(top['amazon']['id'])
            elif top['agreement'] >= 0.4:
                decision = 'REVIEW'
                note = note or ('close runner-up or competing order' if top['agreement'] >= needed
                                else 'partial title agreement')
            if top['amazon']['refund_cents']:
                note = (note + '; ' if note else '') + 'Amazon order has a refund'
        decisions.append({'order': order, 'decision': decision, 'top': top, 'runner': runner, 'candidates': results[:5],
                          'note': note})
    return decisions


def report_rows(decisions):
    for d in sorted(decisions, key=lambda d: (d['decision'], d['order']['created_at'])):
        top = d['top']
        yield {'decision': d['decision'], 'ebay_order_id': d['order']['order_id'], 'ebay_date': str(d['order']['local_day']),
               'ebay_title': d['order']['titles'][:120], 'confidence': top['score'] if top else '',
               'amazon_date': str(top['amazon']['date']) if top else '', 'amazon_to': top['amazon']['to'] if top else '',
               'amazon_items': top['amazon']['items'][:120] if top else '',
               'amazon_total': f"{top['amazon']['total_cents'] / 100:.2f}" if top else '',
               'amazon_refund': f"{top['amazon']['refund_cents'] / 100:.2f}" if top and top['amazon']['refund_cents'] else '',
               'title_agreement': f"{top['agreement']:.2f}" if top else '', 'days_after': top['days'] if top else '',
               'name': top['name'] if top else '', 'runner_up_score': d['runner'] if top else '', 'note': d['note']}


def candidate_json(result):
    amazon = result['amazon']
    return {'gmail_message_id': amazon['id'], 'score': result['score'], 'minutes_after': result['days'] * 1440,
            'received_at': f"{amazon['date']}T12:00:00Z", 'grand_total_cents': amazon['total_cents'],
            'signals': result['signals']}


def apply(db, amazon_orders, decisions, now):
    stamp = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    needed = {c['amazon']['id'] for d in decisions for c in d['candidates']}
    with db:
        for amazon in amazon_orders:
            if amazon['id'] not in needed:
                continue
            # parse_status 'imported': never used by the live email matcher (dates have no time),
            # but listed for manual linking and attachable by this import.
            db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, first_name,
                          quantity, category, grand_total_cents, parse_status, parse_error, body, fetched_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(gmail_message_id) DO NOTHING''',
                       (amazon['id'], f"{amazon['date']}T12:00:00Z", 'Amazon order history (date only): ' + amazon['items'][:150],
                        norm_first_name(amazon['to']) and amazon['to'].split()[0], None, amazon['items'][:120],
                        amazon['total_cents'], 'imported', None, None, stamp))
        for d in decisions:
            if d['decision'] == 'NO_MATCH':
                continue
            match = d['decision'] == 'MATCH'
            db.execute('''INSERT INTO order_matches(order_id, status, gmail_message_id, confidence, candidates_json,
                          method, matched_at, updated_at) VALUES (?,?,?,?,?,?,?,?)
                          ON CONFLICT(order_id) DO UPDATE SET status=excluded.status,
                          gmail_message_id=excluded.gmail_message_id, confidence=excluded.confidence,
                          candidates_json=excluded.candidates_json, method=excluded.method,
                          matched_at=excluded.matched_at, updated_at=excluded.updated_at
                          WHERE order_matches.status NOT IN ('MATCHED','MANUAL')''',
                       (d['order']['order_id'], 'MATCHED' if match else 'NEEDS_REVIEW',
                        d['top']['amazon']['id'] if match else None, d['top']['score'],
                        json.dumps([candidate_json(c) for c in d['candidates']]), 'amazon_csv',
                        stamp if match else None, stamp))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('csv', nargs='+', type=Path)
    parser.add_argument('--until', default='2026-07-14', help='Last eBay order date to fill (inclusive)')
    parser.add_argument('--apply', action='store_true', help='Write matches (otherwise dry run)')
    parser.add_argument('--report', type=Path, default=Path('secrets/amazon_import_report.csv'))
    parser.add_argument('--config', default='config.json')
    args = parser.parse_args(argv)
    config = load_config(args.config)
    zone = local_zone(config.accounting.timezone)
    # Orders created before the end of the --until day (local time), as a UTC timestamp.
    day_after = date.fromisoformat(args.until) + timedelta(days=1)
    until_iso = datetime.combine(day_after, time.min, zone).astimezone(timezone.utc).isoformat(
        timespec='seconds').replace('+00:00', 'Z')
    amazon_orders, skipped = read_amazon(args.csv, config.matching.personal_recipients)
    db = connect(config.database)
    try:
        decisions = plan(db, amazon_orders, until_iso, zone)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, REPORT_FIELDS)
            writer.writeheader()
            writer.writerows(report_rows(decisions))
        counts = {k: sum(d['decision'] == k for d in decisions) for k in ('MATCH', 'REVIEW', 'NO_MATCH')}
        print(f'Amazon orders read: {len(amazon_orders)} (skipped: {skipped})')
        print(f'eBay orders without a cost through {args.until}: {len(decisions)} -> {counts}')
        print(f'Review the proposed matches in {args.report}')
        if args.apply:
            backup = backup_database(db, Path(config.database))
            apply(db, amazon_orders, decisions, datetime.now(timezone.utc))
            print(f'Applied. Database backed up first to {backup}.')
        else:
            print('Dry run: nothing was written. Re-run with --apply to save these matches.')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
