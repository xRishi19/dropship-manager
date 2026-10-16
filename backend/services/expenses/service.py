"""Expense CRUD and occurrence math. Dates are local calendar dates (date objects / YYYY-MM-DD).

A one-time expense occurs on start_date. A recurring expense occurs monthly on start_date's day,
clamped to the month's length (the 31st -> Feb 28/29), from start_date through end_date inclusive.
Range ends are exclusive, like order buckets.
"""
import calendar
from datetime import date, datetime, timedelta, timezone

FIELDS = ('id', 'name', 'amount_cents', 'start_date', 'recurring', 'end_date')


def as_date(value):
    """Accept an existing calendar date or parse its ISO representation.

    Args:
        value: Date object, ISO YYYY-MM-DD string or None.

    Returns:
        Date value, or None when the input is None.
    """
    return value if isinstance(value, date) or value is None else date.fromisoformat(value)


def month_first(day, months_ahead=0):
    """Calculate the first date of a month relative to a reference day.

    Args:
        day: Reference calendar date for relative month arithmetic.
        months_ahead: Number of months to advance; negative values move backward.

    Returns:
        Date at the first day of the offset month, including year rollover.
    """
    month = day.month - 1 + months_ahead
    return date(day.year + month // 12, month % 12 + 1, 1)


def occurrences(expense, start_date=None, end_date=None):
    """Expand an expense into charge dates, clamping monthly recurrence to each month's length.

    Occurrence dates of one expense in [start_date, end_date); start_date None = from the beginning.

    end_date must be given for recurring expenses without an end (otherwise they never stop).

    Args:
        expense: Expense record containing amount, start_date, recurring and end_date.
        start_date: Optional inclusive lower charge-date bound as a date or ISO string; None starts from the expense beginning.
        end_date: Optional exclusive upper charge-date bound as a date or ISO string; required for a recurring expense without a stored end.

    Returns:
        List of date objects inside the half-open requested range; unbounded recurring expenses require an end.
    """
    first, last = as_date(expense['start_date']), as_date(expense['end_date'])
    start_date, end_date = as_date(start_date), as_date(end_date)
    inside = lambda d: (start_date is None or d >= start_date) and (end_date is None or d < end_date)
    if not expense['recurring']:
        return [first] if inside(first) else []
    if last is None and end_date is None:
        raise ValueError('A recurring expense without an end needs a range end date')
    stop = min(d for d in (last and last + timedelta(days=1), end_date) if d)  # Exclusive.
    month = month_first(max(first, start_date) if start_date else first)
    result = []
    while month < stop:
        day = month.replace(day=min(first.day, calendar.monthrange(month.year, month.month)[1]))
        if day >= first and day < stop and inside(day):
            result.append(day)
        month = month_first(month, 1)
    return result


def expenses_in_range(db, start_date, end_date):
    """Sum every stored expense occurrence within local calendar date bounds.

    Total cents of all occurrences in [start_date, end_date); start_date None = from the beginning.

    Args:
        db: Open SQLite connection used for the query or write.
        start_date: Optional inclusive lower charge-date bound; None includes expenses from their beginning.
        end_date: Exclusive upper charge-date bound, needed to bound ongoing recurring expenses.

    Returns:
        Total expense amount in integer cents.
    """
    return sum(row['amount_cents'] * len(occurrences(row, start_date, end_date)) for row in all_rows(db))


def all_rows(db):
    """Load stored expense fields in reverse date and identifier order.

    Args:
        db: Open SQLite connection used for the query or write.

    Returns:
        List of SQLite expense rows.
    """
    return db.execute(f'SELECT {", ".join(FIELDS)} FROM expenses ORDER BY start_date DESC, id DESC').fetchall()


def next_occurrence(expense, today):
    """Find the next charge date on or after today without altering the expense.

    First occurrence on or after today, or None.

    Args:
        expense: Expense record containing amount, start_date, recurring and end_date.
        today: Current local calendar date, accepted as a date object or ISO date string.

    Returns:
        Date of the next occurrence, or None when no future charge exists.
    """
    today = as_date(today)
    if not expense['recurring']:
        found = occurrences(expense, today, None)
    else:  # Any occurrence >= today falls within the next two months.
        found = occurrences(expense, today, month_first(max(today, as_date(expense['start_date'])), 2))
    return found[0] if found else None


def serialize(row, today):
    """Prepare an expense row for the API and attach its next charge date.

    Args:
        row: SQLite expense row with all public expense fields.
        today: Current local calendar date, accepted as a date object or ISO date string.

    Returns:
        Dictionary of expense fields with a boolean recurring flag and nullable ISO next_occurrence.
    """
    item = {key: row[key] for key in FIELDS}
    item['recurring'] = bool(item['recurring'])
    upcoming = next_occurrence(row, today)
    item['next_occurrence'] = upcoming.isoformat() if upcoming else None
    return item


def list_expenses(db, today):
    """Build the expense-list response and full-current-month expense summaries.

    {expenses, this_month_cents, monthly_recurring_cents}; "this month" is today's full local month.

    Args:
        db: Open SQLite connection used for the query or write.
        today: Current local calendar date, accepted as a date object or ISO date string.

    Returns:
        Dictionary with expenses, this_month_cents and monthly_recurring_cents.
    """
    today = as_date(today)
    month, after = month_first(today), month_first(today, 1)
    rows = all_rows(db)
    this_month = sum(r['amount_cents'] * len(occurrences(r, month, after)) for r in rows)
    recurring = sum(r['amount_cents'] for r in rows if r['recurring'] and occurrences(r, month, after))
    return {'expenses': [serialize(r, today) for r in rows], 'this_month_cents': this_month,
            'monthly_recurring_cents': recurring}


def ordering_fee_summary(db, today, zone, per_order_cents):
    """Count per-order provider fees by local creation date without adding them to shared expenses.

    Ordering-provider fees: every eBay order in the database (cancelled ones included), counted by local creation
    date for this month, year to date and all time. Informational only: the fee is already in each order's profit,
    so it is never added to expenses.

    Args:
        db: Open SQLite connection used for the query or write.
        today: Current local calendar date, accepted as a date object or ISO date string.
        zone: Accounting time-zone object used for local calendar boundaries.
        per_order_cents: Configured ordering-provider fee charged per eBay order.

    Returns:
        Dictionary with per_order_cents and this-month, year-to-date and all-time counts/cent totals.
    """
    today = as_date(today)
    month, year = month_first(today), date(today.year, 1, 1)
    counts = {'this_month': 0, 'ytd': 0, 'all': 0}
    for (created,) in db.execute('SELECT created_at FROM orders'):
        day = datetime.fromisoformat(created.replace('Z', '+00:00')).astimezone(zone).date()
        counts['all'] += 1
        counts['ytd'] += year <= day <= today
        counts['this_month'] += month <= day <= today
    result = {'per_order_cents': per_order_cents}
    for period, count in counts.items():
        result[f'{period}_count'] = count
        result[f'{period}_cents'] = count * per_order_cents
    return result


def get(db, expense_id):
    """Look up the public fields of a stored expense by its identifier.

    Args:
        db: Open SQLite connection used for the query or write.
        expense_id: Integer identifier of the stored expense.

    Returns:
        SQLite row, or None when no expense exists.
    """
    return db.execute(f'SELECT {", ".join(FIELDS)} FROM expenses WHERE id=?', (expense_id,)).fetchone()


def stamp():
    """Generate the current UTC timestamp for expense audit fields.

    Args:
        None.

    Returns:
        Second-resolution ISO timestamp ending in Z.
    """
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def create(db, name, amount_cents, start_date, recurring, end_date):
    """Insert a validated expense and initialize its audit timestamps. Caller owns the transaction.

    Args:
        db: Open SQLite connection used for the query or write.
        name: Validated expense display name.
        amount_cents: Validated expense amount in integer cents.
        start_date: Validated first charge date as an ISO YYYY-MM-DD string.
        recurring: Whether the expense repeats each month on its start-date day.
        end_date: Optional inclusive final recurrence date as ISO YYYY-MM-DD.

    Returns:
        Integer identifier of the newly inserted expense.
    """
    now = stamp()
    cursor = db.execute('INSERT INTO expenses(name, amount_cents, start_date, recurring, end_date, created_at, updated_at) '
                        'VALUES (?,?,?,?,?,?,?)', (name, amount_cents, start_date, int(recurring), end_date, now, now))
    return cursor.lastrowid


def update(db, expense_id, name, amount_cents, start_date, recurring, end_date):
    """Replace a stored expense's editable fields and update its audit timestamp. Caller owns the transaction.

    True if the expense existed.

    Args:
        db: Open SQLite connection used for the query or write.
        expense_id: Integer identifier of the stored expense.
        name: Validated expense display name.
        amount_cents: Validated expense amount in integer cents.
        start_date: Validated first charge date as an ISO YYYY-MM-DD string.
        recurring: Whether the expense repeats each month on its start-date day.
        end_date: Optional inclusive final recurrence date as ISO YYYY-MM-DD.

    Returns:
        True when the expense existed and was updated, otherwise False.
    """
    cursor = db.execute('UPDATE expenses SET name=?, amount_cents=?, start_date=?, recurring=?, end_date=?, updated_at=? '
                        'WHERE id=?', (name, amount_cents, start_date, int(recurring), end_date, stamp(), expense_id))
    return cursor.rowcount > 0


def delete(db, expense_id):
    """Remove an expense by identifier. Caller owns the transaction.

    True if the expense existed.

    Args:
        db: Open SQLite connection used for the query or write.
        expense_id: Integer identifier of the stored expense.

    Returns:
        True when a row was deleted, otherwise False.
    """
    return db.execute('DELETE FROM expenses WHERE id=?', (expense_id,)).rowcount > 0
