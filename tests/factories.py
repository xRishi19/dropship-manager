"""Builders for eBay/Gmail payloads used by the accounting tests (synthetic, no real data)."""


def money(value, currency='USD'):
    return {'value': f'{value:.2f}', 'currency': currency}


def ebay_order(order_id, created='2026-09-20T15:00:00.000Z', items=None, *, name='Jane Doe', city='Springfield',
               state='IL', total=50.0, due=45.0, fee=6.0, tax=5.0, cancelled=False, payment='PAID',
               refunds=None, modified=None):
    items = items or [('111', 'Lug Nuts 20pc M12', 1, 'v0a1b2c3dQjBURVNUQVNJTg')]
    line_items = []
    for index, (item_id, title, quantity, sku) in enumerate(items):
        line = {'lineItemId': f'{order_id}-{index}', 'legacyItemId': item_id, 'title': title,
                'quantity': quantity, 'sku': sku, 'total': money(total / len(items))}
        if tax and index == 0:
            line['ebayCollectAndRemitTaxes'] = [{'amount': money(tax), 'taxType': 'STATE_SALES_TAX'}]
        line_items.append(line)
    return {'orderId': order_id, 'creationDate': created, 'lastModifiedDate': modified or created,
            'orderFulfillmentStatus': 'FULFILLED', 'orderPaymentStatus': payment,
            'cancelStatus': {'cancelState': 'CANCELED' if cancelled else 'NONE_REQUESTED'},
            'buyer': {'username': 'buyer_' + order_id},
            'pricingSummary': {'total': money(total)}, 'totalMarketplaceFee': money(fee),
            'paymentSummary': {'totalDueSeller': money(due), 'refunds': refunds or []},
            'fulfillmentStartInstructions': [{'shippingStep': {'shipTo': {
                'fullName': name, 'contactAddress': {'addressLine1': '1 Main St', 'city': city,
                                                     'stateOrProvince': state, 'postalCode': '62704',
                                                     'countryCode': 'US'}}}}],
            'lineItems': line_items}


def sale_transaction(order_id, net, *, gross=50.0, fvf=6.0, ad=0.0, tax=5.0, transaction_id=None,
                     date='2026-09-20T16:00:00.000Z'):
    fees = [{'feeType': 'FINAL_VALUE_FEE', 'amount': money(fvf)}]
    if ad:
        fees.append({'feeType': 'AD_FEE', 'amount': money(ad)})
    return {'transactionId': transaction_id or f'sale-{order_id}', 'transactionType': 'SALE', 'orderId': order_id,
            'transactionDate': date, 'bookingEntry': 'CREDIT', 'amount': money(net),
            'totalFeeBasisAmount': money(gross), 'ebayCollectedTaxAmount': money(tax),
            'orderLineItems': [{'lineItemId': f'{order_id}-0', 'marketplaceFees': fees}]}


def charge(order_id, amount, kind='NON_SALE_CHARGE', fee_type='AD_FEE', transaction_id=None,
           date='2026-09-21T10:00:00.000Z'):
    row = {'transactionId': transaction_id or f'{kind}-{order_id}-{amount}', 'transactionType': kind,
           'transactionDate': date, 'bookingEntry': 'DEBIT', 'amount': money(amount),
           'references': [{'referenceId': order_id, 'referenceType': 'ORDER_ID'}]}
    if fee_type:
        row['feeType'] = fee_type
    return row


def amazon_email(db, message_id, received, *, first_name='Jane', city='Springfield', state='IL', quantity=1,
                 total_cents=3000, status='parsed', subject='Ordered: "Lug Nuts"'):
    db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, first_name, city,
                  state, quantity, category, grand_total_cents, parse_status, body, fetched_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
               (message_id, received, subject, first_name, city, state, quantity, None, total_cents, status, '', received))
