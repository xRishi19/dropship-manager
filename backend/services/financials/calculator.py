"""Pure profit rules. Raw eBay/Amazon values go in; effective (displayed) values come out.
Nothing here mutates stored data, so raw history is never lost when a status changes.

- net revenue is what eBay pays out (after fees, ad fees, eBay-collected tax, refunds)
- original profit = net revenue - Amazon cost - ordering-provider fee (unknown when revenue or
  cost is unknown; never guessed)
- cancelled, RETURN_COMPLETED and REFUNDED orders: effective revenue and cost are 0, effective
  profit is -fee (the provider fee was paid anyway), and the order is excluded from the order
  count and revenue; original values stay available for audit
- RETURN_STARTED keeps the profit and flags the pending return; RETURN_CANCELLED is normal
"""
from dataclasses import asdict, dataclass, field

from ...models.statuses import RETURN_STATUSES, ZEROED_RETURNS  # noqa: F401  (re-exported)


@dataclass
class Financials:
    net_revenue_cents: int | None
    amazon_cost_cents: int | None
    original_profit_cents: int | None
    effective_revenue_cents: int | None
    effective_cost_cents: int | None
    effective_profit_cents: int | None
    excluded: bool  # Left out of revenue/cost/profit totals (cancelled, returned, refunded).
    cost_known: bool
    revenue_estimated: bool
    display_status: str
    flags: list = field(default_factory=list)
    margin: float | None = None  # effective profit / effective revenue
    roi: float | None = None  # effective profit / effective cost
    ordering_fee_cents: int = 0  # Ordering-provider fee charged on this order (already in profit).

    def as_dict(self):
        return asdict(self)


def amazon_cost(manual_cost_cents, match_status, email_total_cents):
    """A manually entered cost wins; otherwise a matched/manually linked email's Grand Total."""
    if manual_cost_cents is not None:
        return manual_cost_cents
    if match_status in {'MATCHED', 'MANUAL'} and email_total_cents is not None:
        return email_total_cents
    return None


def compute(*, is_cancelled, return_status, net_revenue_cents, amazon_cost_cents, revenue_source=None,
            ordering_fee_cents=0):
    if return_status not in RETURN_STATUSES:
        raise ValueError(f'Unknown return status {return_status!r}')
    known = net_revenue_cents is not None and amazon_cost_cents is not None
    original = net_revenue_cents - amazon_cost_cents - ordering_fee_cents if known else None
    flags = []
    if revenue_source == 'fulfillment_estimate':
        flags.append('REVENUE_ESTIMATED')
    if amazon_cost_cents is None:
        flags.append('COST_UNKNOWN')
    if is_cancelled:
        status, excluded = 'Cancelled', True
    elif return_status == 'RETURN_COMPLETED':
        status, excluded = 'Returned', True
    elif return_status == 'REFUNDED':
        status, excluded = 'Refunded', True
    else:
        status, excluded = ('Return pending' if return_status == 'RETURN_STARTED' else 'Completed'), False
        if return_status == 'RETURN_STARTED':
            flags.append('RETURN_PENDING')
    if excluded:
        revenue = cost = 0
        profit = -ordering_fee_cents
    else:
        revenue, cost, profit = net_revenue_cents, amazon_cost_cents, original
    return Financials(
        net_revenue_cents=net_revenue_cents, amazon_cost_cents=amazon_cost_cents,
        original_profit_cents=original, effective_revenue_cents=revenue, effective_cost_cents=cost,
        effective_profit_cents=profit, excluded=excluded, cost_known=amazon_cost_cents is not None,
        revenue_estimated=revenue_source == 'fulfillment_estimate', display_status=status, flags=flags,
        margin=round(profit / revenue, 4) if profit is not None and revenue else None,
        roi=round(profit / cost, 4) if profit is not None and cost else None,
        ordering_fee_cents=ordering_fee_cents)
