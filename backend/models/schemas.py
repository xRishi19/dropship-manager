"""Request bodies accepted by the HTTP API."""
from pydantic import BaseModel


class CostBody(BaseModel):
    amount: str | None  # Dollars, e.g. "23.45"; null clears the manual cost.


class LinkBody(BaseModel):
    gmail_message_id: str
    replace: bool = False  # Move the email from another order to this one.


class ReturnBody(BaseModel):
    status: str | None  # One of RETURN_STATUSES; null restores the eBay-derived status.


class RunBody(BaseModel):
    confirm: bool = False  # The UI must confirm: this calls the OpenAI API.
    refresh_listings: bool = True


class ExpenseBody(BaseModel):
    name: str
    amount: str | int | float  # Dollars, e.g. "12.34" or "$1,234.56".
    date: str  # YYYY-MM-DD (local): the one-time date, or the first charge of a monthly recurring expense.
    recurring: bool = False  # Monthly on date's day of the month (clamped to short months).
    end_date: str | None = None  # YYYY-MM-DD, inclusive; set it to stop a subscription.
