"""Builders for eBay/Gmail payloads used by the accounting tests (synthetic, no real data)."""


def money(value, currency='USD'):
    """Build an eBay Amount dictionary with two-decimal string precision.

    Args:
        value: Amount in currency units formatted to two decimal places.
        currency: ISO currency code used by the synthetic eBay Amount, defaulting to USD.

    Returns:
        Dictionary containing value as a decimal string and the supplied currency code.
    """
    return {'value': f'{value:.2f}', 'currency': currency}


def ebay_order(order_id, created='2026-09-20T15:00:00.000Z', items=None, *, name='Jane Doe', city='Springfield',
               state='IL', total=50.0, due=45.0, fee=6.0, tax=5.0, cancelled=False, payment='PAID',
               refunds=None, modified=None):
    """Build a complete synthetic Fulfillment order including lines, recipient, payment, fees, and cancellation data.

    Args:
        order_id: Synthetic eBay or Amazon order identifier used to distinguish test records.
        created: ISO order creation timestamp; also used as lastModifiedDate when modified is
            omitted.
        items: Optional (item_id, title, quantity, sku) tuples; omitted or empty values select
            the default product.
        name: Synthetic full recipient name in the shipping instructions.
        city: Synthetic destination city used by order matching.
        state: Synthetic destination state used by order matching.
        total: Gross order amount in currency units, divided equally across synthetic lines.
        due: Seller amount before marketplace fees, in currency units.
        fee: Total marketplace fee in currency units for the initial Fulfillment estimate.
        tax: eBay-collected sales tax in currency units.
        cancelled: Boolean selecting CANCELED versus NONE_REQUESTED in the order cancellation
            status.
        payment: Synthetic eBay order payment status, defaulting to PAID.
        refunds: Optional refund payloads included in the eBay payment summary.
        modified: Optional last-modified timestamp; None falls back to the creation timestamp.

    Returns:
        eBay-style order dictionary ready for store_orders or a mocked Fulfillment response.
    """
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
    """Build a synthetic credited SALE transaction with final-value and optional advertising fees.

    Args:
        order_id: Synthetic eBay or Amazon order identifier used to distinguish test records.
        net: Net eBay SALE transaction amount in currency units.
        gross: Gross fee-basis amount in currency units.
        fvf: Final-value fee in currency units included in the SALE transaction.
        ad: Advertising fee in currency units included in the SALE transaction.
        tax: eBay-collected sales tax in currency units.
        transaction_id: Optional synthetic eBay transaction identifier; None selects the
            factory-generated key.
        date: ISO transaction timestamp inserted into the synthetic financial payload.

    Returns:
        eBay Finances transaction dictionary with order and line references.
    """
    fees = [{'feeType': 'FINAL_VALUE_FEE', 'amount': money(fvf)}]
    if ad:
        fees.append({'feeType': 'AD_FEE', 'amount': money(ad)})
    return {'transactionId': transaction_id or f'sale-{order_id}', 'transactionType': 'SALE', 'orderId': order_id,
            'transactionDate': date, 'bookingEntry': 'CREDIT', 'amount': money(net),
            'totalFeeBasisAmount': money(gross), 'ebayCollectedTaxAmount': money(tax),
            'orderLineItems': [{'lineItemId': f'{order_id}-0', 'marketplaceFees': fees}]}


def charge(order_id, amount, kind='NON_SALE_CHARGE', fee_type='AD_FEE', transaction_id=None,
           date='2026-09-21T10:00:00.000Z'):
    """Build a synthetic debit transaction referenced to an order, optionally identifying its fee type.

    Args:
        order_id: Synthetic eBay or Amazon order identifier used to distinguish test records.
        amount: Synthetic debit amount in currency units.
        kind: eBay transaction-type string, defaulting to NON_SALE_CHARGE.
        fee_type: Optional eBay debit fee type; None omits the feeType payload field.
        transaction_id: Optional synthetic eBay transaction identifier; None selects the
            factory-generated key.
        date: ISO transaction timestamp inserted into the synthetic financial payload.

    Returns:
        eBay Finances transaction dictionary with bookingEntry set to DEBIT.
    """
    row = {'transactionId': transaction_id or f'{kind}-{order_id}-{amount}', 'transactionType': kind,
           'transactionDate': date, 'bookingEntry': 'DEBIT', 'amount': money(amount),
           'references': [{'referenceId': order_id, 'referenceType': 'ORDER_ID'}]}
    if fee_type:
        row['feeType'] = fee_type
    return row


def amazon_email(db, message_id, received, *, first_name='Jane', city='Springfield', state='IL', quantity=1,
                 total_cents=3000, status='parsed', subject='Ordered: "Lug Nuts"'):
    """Insert a synthetic Amazon confirmation purchase row directly into the test database; the caller commits the transaction.

    Args:
        db: Migrated SQLite connection supplied by the test fixture; used to seed or inspect
            isolated records.
        message_id: Synthetic Gmail message identifier used as the purchase record key.
        received: ISO timestamp stored as both the email receipt and initial fetch time.
        first_name: Synthetic recipient first name used by order matching.
        city: Synthetic destination city used by order matching.
        state: Synthetic destination state used by order matching.
        quantity: Parameterized quantity or synthetic units ordered; malformed cases exercise
            input validation.
        total_cents: Amazon confirmation grand total in integer cents.
        status: Parse status stored on the synthetic email purchase, defaulting to parsed.
        subject: Synthetic Amazon confirmation subject consumed by the email parser.

    Returns:
        None. Inserts the purchase row without contacting Gmail.
    """
    db.execute('''INSERT INTO amazon_email_purchases(gmail_message_id, received_at, subject, first_name, city,
                  state, quantity, category, grand_total_cents, parse_status, body, fetched_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
               (message_id, received, subject, first_name, city, state, quantity, None, total_cents, status, '', received))
