"""Pure translations of eBay API payloads into database rows. No network or database access.

Field names follow the Fulfillment getOrders, Finances getTransactions and Post-Order
return/search payloads. Mappings marked VERIFY should be checked against real account
payloads (redacted samples belong in tests/fixtures/).
"""
import json
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from ...db.ingest import asin_from_sku

# Fee types rolled into the "final value fee" column; everything else is kept by type.
FINAL_VALUE_FEES = {'FINAL_VALUE_FEE', 'FINAL_VALUE_FEE_FIXED_PER_ORDER', 'FINAL_VALUE_SHIPPING_FEE'}
AD_FEE_TYPES = {'AD_FEE', 'PROMOTED_LISTING_FEE', 'PROMOTED_LISTINGS_FEE', 'PROMOTED_LISTING_ADVANCED_FEE'}


def cents(amount):
    """eBay Amount ({'value': '12.34', 'currency': 'USD'}) or plain value -> integer cents."""
    if amount is None:
        return None
    value = amount.get('value') if isinstance(amount, dict) else amount
    if value in (None, ''):
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return None


def currency(amount):
    return amount.get('currency') if isinstance(amount, dict) else None


def iso_utc(value):
    """eBay ISO timestamp -> canonical second-precision UTC ('2026-01-01T05:00:00Z')."""
    if not value:
        return value
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def first_name(full_name):
    parts = (full_name or '').split()
    return parts[0] if parts else None


def parse_order(order, synced_at):
    """Fulfillment order -> (orders row, order_items rows, estimated financial record)."""
    ship_to = {}
    for instruction in order.get('fulfillmentStartInstructions') or []:
        ship_to = (instruction.get('shippingStep') or {}).get('shipTo') or {}
        if ship_to:
            break
    address = ship_to.get('contactAddress') or {}
    cancel_state = (order.get('cancelStatus') or {}).get('cancelState')
    pricing = order.get('pricingSummary') or {}
    row = {
        'order_id': order['orderId'], 'created_at': iso_utc(order['creationDate']),
        'last_modified_at': iso_utc(order.get('lastModifiedDate')),
        'fulfillment_status': order.get('orderFulfillmentStatus'),
        'payment_status': order.get('orderPaymentStatus'), 'cancel_state': cancel_state,
        'is_cancelled': int(cancel_state == 'CANCELED'),
        'buyer_username': (order.get('buyer') or {}).get('username'),
        'ship_name': ship_to.get('fullName'), 'ship_first_name': first_name(ship_to.get('fullName')),
        'address1': address.get('addressLine1'), 'address2': address.get('addressLine2'),
        'city': address.get('city'), 'state': address.get('stateOrProvince'),
        'postal_code': address.get('postalCode'), 'country': address.get('countryCode'),
        'currency': currency(pricing.get('total')), 'raw_json': json.dumps(order, sort_keys=True),
        'synced_at': synced_at,
    }
    items, tax = [], 0
    for item in order.get('lineItems') or []:
        sku = item.get('sku') or ''
        items.append({'order_id': row['order_id'], 'line_item_id': item['lineItemId'],
                      'item_id': item['legacyItemId'], 'sku': sku, 'asin': asin_from_sku(sku),
                      'title': item.get('title') or '', 'quantity': int(item.get('quantity') or 0),
                      'line_total_cents': cents(item.get('total') or item.get('lineItemCost')),
                      'raw_json': json.dumps(item, sort_keys=True)})
        tax += sum(cents(t.get('amount')) or 0 for t in item.get('ebayCollectAndRemitTaxes') or [])
    # VERIFY: estimate until the Finances SALE transaction posts. totalDueSeller excludes
    # eBay-collected tax; totalMarketplaceFee is eBay's fee total. Ad fees are unknown here.
    gross = cents(pricing.get('total'))
    due = cents((order.get('paymentSummary') or {}).get('totalDueSeller'))
    fee = cents(order.get('totalMarketplaceFee'))
    base = due if due is not None else (gross - tax if gross is not None else None)
    estimate = {'order_id': row['order_id'], 'currency': row['currency'], 'gross_total_cents': gross,
                'sales_tax_cents': tax, 'final_value_fee_cents': fee, 'other_fees_cents': None,
                'other_fees_json': None, 'ad_fee_cents': None, 'refund_cents': refunds(order),
                'net_revenue_cents': None if base is None else base - (fee or 0),
                'source': 'fulfillment_estimate'}
    return row, items, estimate


def refunds(order):
    total = 0
    for refund in (order.get('paymentSummary') or {}).get('refunds') or []:
        if refund.get('refundStatus', 'REFUNDED') in {'REFUNDED', 'COMPLETED'}:
            total += cents(refund.get('amount')) or 0
    return total


def order_sales_rows(order):
    """The sourcing `sales` view of an order: one row per line, quantity 0 if cancelled."""
    canceled = (order.get('cancelStatus') or {}).get('cancelState') == 'CANCELED'
    return [{'order_id': order['orderId'], 'item_id': item['legacyItemId'], 'line_id': item['lineItemId'],
             'title': item['title'], 'sku': item.get('sku', ''),
             'quantity': 0 if canceled else item['quantity'], 'sold_date': order['creationDate']}
            for item in order.get('lineItems', [])]


def transaction_order_id(transaction):
    if transaction.get('orderId'):
        return transaction['orderId']
    for reference in transaction.get('references') or []:
        if reference.get('referenceType') == 'ORDER_ID':
            return reference.get('referenceId')
    return None


def transaction_row(transaction):
    amount = cents(transaction.get('amount')) or 0
    return {'transaction_id': transaction['transactionId'], 'transaction_type': transaction.get('transactionType', ''),
            'order_id': transaction_order_id(transaction), 'transaction_date': transaction.get('transactionDate', ''),
            'amount_cents': amount, 'booking_entry': transaction.get('bookingEntry'),
            'raw_json': json.dumps(transaction, sort_keys=True)}


def aggregate_finances(transactions):
    """All posted Finances transactions for one order -> authoritative financial fields.
    net revenue = credits - debits (sale net of deducted fees, minus ad fees, refunds and
    other charges), i.e. what eBay actually pays out for the order."""
    sales = [t for t in transactions if t['transaction_type'] == 'SALE']
    if not sales:
        return None
    net = fvf = other = ad = refund = gross = tax = 0
    other_by_type = {}
    for row in transactions:
        raw = json.loads(row['raw_json'])
        signed = row['amount_cents'] if row.get('booking_entry') != 'DEBIT' else -row['amount_cents']
        net += signed
        kind = row['transaction_type']
        if kind == 'SALE':
            gross += cents(raw.get('totalFeeBasisAmount')) or 0
            tax += cents(raw.get('ebayCollectedTaxAmount')) or 0
            for line in raw.get('orderLineItems') or []:
                for fee in line.get('marketplaceFees') or []:
                    value = cents(fee.get('amount')) or 0
                    if fee.get('feeType') in FINAL_VALUE_FEES:
                        fvf += value
                    elif fee.get('feeType') in AD_FEE_TYPES:
                        ad += value
                    else:
                        other += value
                        other_by_type[fee.get('feeType', 'OTHER')] = other_by_type.get(fee.get('feeType', 'OTHER'), 0) + value
        elif kind == 'REFUND':
            refund += row['amount_cents']
        elif kind == 'NON_SALE_CHARGE' and raw.get('feeType') in AD_FEE_TYPES:
            ad += row['amount_cents']
        elif row.get('booking_entry') == 'DEBIT':
            other += row['amount_cents']
            other_by_type[kind] = other_by_type.get(kind, 0) + row['amount_cents']
    first = json.loads(sales[0]['raw_json'])
    return {'currency': currency(first.get('amount')), 'gross_total_cents': gross or None,
            'sales_tax_cents': tax, 'final_value_fee_cents': fvf, 'other_fees_cents': other,
            'other_fees_json': json.dumps(other_by_type, sort_keys=True) if other_by_type else None,
            'ad_fee_cents': ad, 'refund_cents': refund, 'net_revenue_cents': net, 'source': 'finances'}


RETURN_CANCELLED_WORDS = ('CANCEL',)
RETURN_DONE_WORDS = ('CLOSED', 'REFUNDED', 'COMPLETED')


def return_status(member):
    """VERIFY: Post-Order return state/status -> RETURN_STARTED/COMPLETED/CANCELLED."""
    text = ' '.join(str(member.get(k) or '') for k in ('state', 'status')).upper()
    if any(word in text for word in RETURN_CANCELLED_WORDS):
        return 'RETURN_CANCELLED'
    refunded = cents(((member.get('sellerTotalRefund') or {}).get('actualRefundAmount'))) or 0
    if any(word in text for word in RETURN_DONE_WORDS) and refunded:
        return 'RETURN_COMPLETED'
    if 'CLOSED' in text:
        return 'RETURN_CANCELLED'  # Closed without a refund: the sale stands.
    return 'RETURN_STARTED'

