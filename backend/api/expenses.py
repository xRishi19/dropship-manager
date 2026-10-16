"""Expenses: one-time and monthly recurring. Validation errors are 400s."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request

from ..models.schemas import ExpenseBody
from ..services.expenses import service
from ..services.financials.aggregation import local_zone
from .deps import get_db, now

router = APIRouter(tags=['expenses'])
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def local_today(request):
    """Resolve today's calendar date in the configured accounting time zone.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.

    Returns:
        Local date object.
    """
    return now().astimezone(local_zone(request.app.state.config.accounting.timezone)).date()


def parse_date(value, field):
    """Validate and normalize an expense date with a field-specific HTTP error.

    Args:
        value: Expense date text required in YYYY-MM-DD format; empty/invalid text raises HTTP 400.
        field: Request field label to include in date-validation errors.

    Returns:
        ISO YYYY-MM-DD string; invalid values raise HTTPException 400.
    """
    try:
        if not DATE.match(value or ''):
            raise ValueError
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, f'{field} must be a date like 2026-09-25') from None


def validated(body):
    """Validate editable expense fields and convert the entered dollar amount to cents.

    (name, amount_cents, start_date, recurring, end_date) or a 400.

    Args:
        body: ExpenseBody with name, dollar amount, first charge date, recurrence flag and optional ending date.

    Returns:
        Tuple (name, amount_cents, start_date, recurring, end_date); invalid fields raise HTTPException 400.
    """
    name = body.name.strip()
    if not name:
        raise HTTPException(400, 'Name is required')
    try:
        amount = Decimal(str(body.amount).strip().replace('$', '').replace(',', ''))
        if not amount.is_finite():
            raise InvalidOperation
        cents = int((amount * 100).to_integral_value())
    except InvalidOperation:
        raise HTTPException(400, 'Amount must be a dollar value like 23.45') from None
    if cents <= 0:
        raise HTTPException(400, 'Amount must be greater than 0')
    start = parse_date(body.date, 'date')
    end = parse_date(body.end_date, 'end_date') if body.end_date not in (None, '') else None
    if end is not None and end < start:
        raise HTTPException(400, 'end_date must be on or after date')
    return name, cents, start, body.recurring, end


def one(request, db, expense_id):
    """Load and serialize an expense with its next local-calendar occurrence.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.
        expense_id: Integer identifier of the stored expense.

    Returns:
        Expense dictionary; missing records raise HTTPException 404.
    """
    row = service.get(db, expense_id)
    if row is None:
        raise HTTPException(404, 'Expense not found')
    return service.serialize(row, local_today(request))


@router.get('/expenses')
def list_expenses(request: Request, db=Depends(get_db)):
    """Serve all expenses, monthly totals and informational ordering-provider fee summaries.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        db: Open SQLite connection used for the query or write.

    Returns:
        Expense-list response dictionary with ordering_fees attached.
    """
    accounting = request.app.state.config.accounting
    today = local_today(request)
    result = service.list_expenses(db, today)
    result['ordering_fees'] = service.ordering_fee_summary(  # Informational; never part of expenses_cents.
        db, today, local_zone(accounting.timezone), getattr(accounting, 'ordering_fee_cents', 30))
    return result


@router.post('/expenses')
def create_expense(request: Request, body: ExpenseBody, db=Depends(get_db)):
    """Validate and commit a new expense, then return its serialized record.

    Args:
        request: FastAPI request providing application configuration and shared runtime services.
        body: ExpenseBody with validated name, amount, date, recurrence and optional ending date.
        db: Open SQLite connection used for the query or write.

    Returns:
        Created expense dictionary including its identifier and next occurrence.
    """
    fields = validated(body)
    with db:
        expense_id = service.create(db, *fields)
    return one(request, db, expense_id)


@router.put('/expenses/{expense_id}')
def update_expense(expense_id: int, request: Request, body: ExpenseBody, db=Depends(get_db)):
    """Validate and commit replacement expense fields for an existing record.

    Args:
        expense_id: Integer identifier of the stored expense.
        request: FastAPI request providing application configuration and shared runtime services.
        body: ExpenseBody replacing name, amount, date, recurrence and optional ending date.
        db: Open SQLite connection used for the query or write.

    Returns:
        Updated expense dictionary; missing identifiers raise HTTPException 404.
    """
    fields = validated(body)
    with db:
        found = service.update(db, expense_id, *fields)
    if not found:
        raise HTTPException(404, 'Expense not found')
    return one(request, db, expense_id)


@router.delete('/expenses/{expense_id}')
def delete_expense(expense_id: int, db=Depends(get_db)):
    """Commit deletion of an existing expense.

    Args:
        expense_id: Integer identifier of the stored expense.
        db: Open SQLite connection used for the query or write.

    Returns:
        Dictionary containing the deleted identifier; missing expenses raise HTTPException 404.
    """
    with db:
        found = service.delete(db, expense_id)
    if not found:
        raise HTTPException(404, 'Expense not found')
    return {'deleted': expense_id}
