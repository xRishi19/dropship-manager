"""Status vocabularies shared by services, API and exports."""

RETURN_STATUSES = ('NONE', 'RETURN_STARTED', 'RETURN_COMPLETED', 'RETURN_CANCELLED', 'REFUNDED')
# Returns whose effective revenue/cost/profit are zero (excluded from totals, originals kept).
ZEROED_RETURNS = frozenset({'RETURN_COMPLETED', 'REFUNDED'})
MATCH_STATUSES = ('MATCHED', 'PENDING', 'NEEDS_REVIEW', 'MANUAL')
REVENUE_SOURCES = ('finances', 'fulfillment_estimate')
