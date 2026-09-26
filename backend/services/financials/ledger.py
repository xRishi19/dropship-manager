"""Loads orders with their raw financial, match and return data and applies the calculator.
The single read path for the dashboard, the orders table and the CSV export."""
import json

from ..returns.service import effective_status as effective_return_status
from .calculator import amazon_cost, compute

ORDER_QUERY = '''
SELECT o.*, f.currency AS fin_currency, f.gross_total_cents, f.sales_tax_cents, f.final_value_fee_cents,
       f.other_fees_cents, f.other_fees_json, f.ad_fee_cents, f.refund_cents, f.net_revenue_cents,
       f.source AS revenue_source,
       m.status AS match_status, m.confidence AS match_confidence, m.gmail_message_id, m.manual_cost_cents,
       m.method AS match_method, m.candidates_json,
       e.grand_total_cents AS email_total_cents, e.received_at AS email_received_at, e.subject AS email_subject,
       r.ebay_status AS ebay_return_status, r.manual_status AS manual_return_status
FROM orders o
LEFT JOIN financial_records f ON f.order_id = o.order_id
LEFT JOIN order_matches m ON m.order_id = o.order_id
LEFT JOIN amazon_email_purchases e ON e.gmail_message_id = m.gmail_message_id
LEFT JOIN returns r ON r.order_id = o.order_id
'''


def load_orders(db, start=None, end=None, order_ids=None, ordering_fee_cents=0):
    """Orders created in [start, end) (UTC ISO strings), newest first, with financials applied
    (each order's profit includes the ordering-provider fee, config accounting.ordering_fee_cents)."""
    clauses, params = [], []
    if start:
        clauses.append('o.created_at >= ?')
        params.append(start)
    if end:
        clauses.append('o.created_at < ?')
        params.append(end)
    if order_ids is not None:
        order_ids = list(order_ids)
        if not order_ids:
            return []
        clauses.append(f'o.order_id IN ({",".join("?" * len(order_ids))})')
        params += order_ids
    where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = [dict(r) for r in db.execute(ORDER_QUERY + where + ' ORDER BY o.created_at DESC', params)]
    items = load_items(db, [r['order_id'] for r in rows])
    for row in rows:
        row['items'] = items.get(row['order_id'], [])
        apply_financials(row, ordering_fee_cents)
    return rows


def load_items(db, order_ids):
    result = {}
    for start in range(0, len(order_ids), 500):
        chunk = order_ids[start:start + 500]
        for item in db.execute(f'''SELECT oi.order_id, oi.line_item_id, oi.item_id, oi.sku, oi.asin, oi.title,
                oi.quantity, oi.line_total_cents, nullif(l.category, '') AS category
                FROM order_items oi LEFT JOIN listings l ON l.item_id = oi.item_id
                WHERE oi.order_id IN ({",".join("?" * len(chunk))}) ORDER BY oi.line_item_id''', chunk):
            result.setdefault(item['order_id'], []).append(dict(item))
    return result


def apply_financials(row, ordering_fee_cents=0):
    row['return_status'] = effective_return_status(row.get('ebay_return_status'), row.get('manual_return_status'),
                                                   row.get('payment_status'))
    row['match_status'] = row.get('match_status') or 'PENDING'
    cost = amazon_cost(row.get('manual_cost_cents'), row['match_status'], row.get('email_total_cents'))
    financials = compute(is_cancelled=bool(row['is_cancelled']), return_status=row['return_status'],
                         net_revenue_cents=row.get('net_revenue_cents'), amazon_cost_cents=cost,
                         revenue_source=row.get('revenue_source'), ordering_fee_cents=ordering_fee_cents)
    row.update(financials.as_dict())
    row['quantity'] = sum(i['quantity'] for i in row['items'])
    first = row['items'][0] if row['items'] else {}
    extra = len(row['items']) - 1
    row['product_title'] = (first.get('title') or '') + (f' (+{extra} more)' if extra > 0 else '')
    row['category'] = next((i['category'] for i in row['items'] if i.get('category')), None)
    row['asin'] = next((i['asin'] for i in row['items'] if i.get('asin')), None)
    row['other_fees'] = json.loads(row['other_fees_json']) if row.get('other_fees_json') else {}
    row['candidates'] = json.loads(row['candidates_json']) if row.get('candidates_json') else []
    return row
