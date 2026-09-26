import csv
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from backend.services.financials.ledger import load_orders
from backend.services.reconciliation import manual
from backend.services.sync.ebay_sync import store_orders
from backend.tools.import_amazon_history import apply, plan, read_amazon
from factories import ebay_order

NY = ZoneInfo('America/New_York')
NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
FIELDS = ['order id', 'order url', 'items', 'to', 'date', 'total', 'shipping', 'shipping_refund', 'gift', 'tax',
          'refund', 'payments']


def write_csv(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, '') for f in FIELDS})
    return path


def amazon(order_id, items, day, total, to='Alex Buyer', refund=''):
    return {'order id': order_id, 'items': items + '; ', 'to': to, 'date': day, 'total': total, 'refund': refund}


def test_personal_orders_summary_rows_and_blanks_are_skipped(tmp_path):
    path = write_csv(tmp_path / 'a.csv', [
        amazon('111-1', 'Lug Nuts 20pc', '2026-03-01', '12.50'),
        amazon('111-2', 'Vitamins', '2026-03-01', '9.00', to='Pat Owner'),
        amazon('111-3', 'Shoes', '2026-03-01', '40.00', to='SAM O'),
        amazon('111-4', 'Pending order', '2026-03-02', ''),
        {'order id': '', 'total': '=SUBTOTAL(109F2:F164)'}])
    orders, skipped = read_amazon([path], ['pat', 'patricia', 'sam'])
    assert [o['items'] for o in orders] == ['Lug Nuts 20pc']
    assert skipped == {'personal': 2, 'no total or date': 2, 'duplicate': 0}
    assert '111-1' not in orders[0]['id']  # The Amazon order ID itself is never kept.


def seed(db, *orders):
    with db:
        store_orders(db, list(orders), NOW)


def test_title_and_date_match_old_nameless_orders_and_apply_sets_cost(db, tmp_path):
    seed(db, ebay_order('A', created='2026-03-01T15:00:00.000Z', name=None,
                        items=[('1', 'GE RTV108 Silicone Sealant OEM Replacement WB01X46528', 1, '')]),
         ebay_order('B', created='2026-03-01T16:00:00.000Z', name=None,
                    items=[('2', 'Emraw No 2 HB Translucent Pencils Multipoint', 1, '')]))
    path = write_csv(tmp_path / 'a.csv', [
        amazon('1', 'GE RTV108 Silicone Sealant - OEM Replacement (WB01X46528)', '2026-03-02', '18.35'),
        amazon('2', 'Emraw No 2 HB Translucent Pencils Multipoint Non-Sharpening', '2026-03-01', '8.67'),
        amazon('3', 'Garden Hose 50ft', '2026-03-01', '30.00')])
    orders, _ = read_amazon([path], [])
    decisions = {d['order']['order_id']: d for d in plan(db, orders, '2026-07-15T04:00:00Z', NY)}
    assert {k: d['decision'] for k, d in decisions.items()} == {'A': 'MATCH', 'B': 'MATCH'}
    assert decisions['A']['top']['days'] == 1
    apply(db, orders, list(decisions.values()), NOW)
    costs = {o['order_id']: (o['match_status'], o['amazon_cost_cents']) for o in load_orders(db)}
    assert costs == {'A': ('MATCHED', 1835), 'B': ('MATCHED', 867)}
    # Idempotent: a second run finds nothing left to fill.
    assert plan(db, orders, '2026-07-15T04:00:00Z', NY) == []


def test_ambiguous_brand_conflicts_and_settled_orders_are_left_for_review(db, tmp_path):
    seed(db, ebay_order('A', created='2026-03-01T15:00:00.000Z', name=None,
                        items=[('1', 'EZclicker Big Button Universal TV Remote Control for All TVs', 1, '')]),
         ebay_order('B', created='2026-03-01T15:00:00.000Z', name=None,
                    items=[('2', 'Blue Dog Harness Leash Set Small No Pull', 1, '')]),
         ebay_order('C', created='2026-03-01T15:00:00.000Z', name=None,
                    items=[('3', 'Blue Dog Harness Leash Set Small No Pull', 1, '')]),
         ebay_order('D', created='2026-03-01T15:00:00.000Z', name=None,
                    items=[('4', 'Weber Spirit Locking Caster 4 Pack', 1, '')]))
    with db:
        manual.set_manual_cost(db, 'D', 999, NOW)  # Settled by hand: never touched.
    path = write_csv(tmp_path / 'a.csv', [
        amazon('1', 'Samsung Replacement TV Remote Control for All Samsung TVs', '2026-03-01', '9.99'),
        amazon('2', 'Blue Dog Harness Leash Set Small No Pull Reflective', '2026-03-01', '10.81'),
        amazon('4', 'Weber Spirit Locking Caster 4 Pack', '2026-03-01', '27.96')])
    orders, _ = read_amazon([path], [])
    decisions = {d['order']['order_id']: d for d in plan(db, orders, '2026-07-15T04:00:00Z', NY)}
    assert set(decisions) == {'A', 'B', 'C'}
    assert decisions['A']['decision'] == 'REVIEW' and 'brand differs' in decisions['A']['note']
    assert {decisions['B']['decision'], decisions['C']['decision']} == {'REVIEW'}  # Two orders, one Amazon order.
    apply(db, orders, list(decisions.values()), NOW)
    statuses = {o['order_id']: (o['match_status'], o['amazon_cost_cents']) for o in load_orders(db)}
    assert statuses == {'A': ('NEEDS_REVIEW', None), 'B': ('NEEDS_REVIEW', None), 'C': ('NEEDS_REVIEW', None),
                        'D': ('MANUAL', 999)}


def test_a_different_recipient_name_rules_out_a_candidate(db, tmp_path):
    seed(db, ebay_order('A', created='2026-07-01T15:00:00.000Z', name='Jamie Fox',
                        items=[('1', 'Emraw No 2 HB Translucent Pencils Multipoint', 1, '')]))
    path = write_csv(tmp_path / 'a.csv', [
        amazon('1', 'Emraw No 2 HB Translucent Pencils Multipoint', '2026-07-01', '8.67', to='Chris Doe')])
    orders, _ = read_amazon([path], [])
    [decision] = plan(db, orders, '2026-07-15T04:00:00Z', NY)
    assert decision['decision'] == 'NO_MATCH'
