"""Accounting/tax CSV. Every order appears, including cancelled/returned/refunded ones with
their statuses; profit columns include the ordering-provider fee; money columns are dollars with two decimals."""
from ..sourcing.output import csv_text

EXPORT_FIELDS = [
    'sale_date', 'product_title', 'category', 'quantity', 'asin', 'ebay_item_id', 'ebay_order_id',
    'buyer_name', 'city', 'state', 'zip', 'country', 'ebay_status', 'return_status', 'display_status',
    'gross_ebay_order_total', 'ebay_sales_tax', 'ebay_final_value_fee', 'ebay_ad_fee',
    'ebay_other_deductions', 'ebay_refunds', 'net_ebay_revenue', 'revenue_source', 'amazon_cost', 'ordering_fee',
    'original_profit', 'effective_profit', 'profit_margin', 'roi', 'amazon_match_status',
    'amazon_match_confidence', 'currency']


def dollars(value):
    return '' if value is None else f'{value / 100:.2f}'


def export_rows(orders):
    for order in orders:
        yield {
            'sale_date': order['created_at'], 'product_title': order['product_title'],
            'category': order.get('category') or '', 'quantity': order['quantity'], 'asin': order.get('asin') or '',
            'ebay_item_id': ';'.join(i['item_id'] for i in order['items']), 'ebay_order_id': order['order_id'],
            'buyer_name': order.get('ship_name') or '', 'city': order.get('city') or '',
            'state': order.get('state') or '', 'zip': order.get('postal_code') or '',
            'country': order.get('country') or '',
            'ebay_status': 'CANCELLED' if order['is_cancelled'] else (order.get('fulfillment_status') or ''),
            'return_status': order['return_status'], 'display_status': order['display_status'],
            'gross_ebay_order_total': dollars(order.get('gross_total_cents')),
            'ebay_sales_tax': dollars(order.get('sales_tax_cents')),
            'ebay_final_value_fee': dollars(order.get('final_value_fee_cents')),
            'ebay_ad_fee': dollars(order.get('ad_fee_cents')),
            'ebay_other_deductions': dollars(order.get('other_fees_cents')),
            'ebay_refunds': dollars(order.get('refund_cents')),
            'net_ebay_revenue': dollars(order.get('net_revenue_cents')),
            'revenue_source': order.get('revenue_source') or '',
            'amazon_cost': dollars(order['amazon_cost_cents']),
            'ordering_fee': dollars(order.get('ordering_fee_cents', 0)),
            'original_profit': dollars(order['original_profit_cents']),
            'effective_profit': dollars(order['effective_profit_cents']),
            'profit_margin': '' if order['margin'] is None else f'{order["margin"]:.4f}',
            'roi': '' if order['roi'] is None else f'{order["roi"]:.4f}',
            'amazon_match_status': order['match_status'],
            'amazon_match_confidence': '' if order.get('match_confidence') is None else f'{order["match_confidence"]:.0f}',
            'currency': order.get('currency') or order.get('fin_currency') or ''}


def export_csv(orders):
    """Spreadsheet-safe CSV (formula-like text is escaped, as in the sourcing exports)."""
    return csv_text(list(export_rows(orders)), EXPORT_FIELDS)
