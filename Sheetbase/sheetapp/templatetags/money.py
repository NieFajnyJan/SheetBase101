from datetime import date
from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def zl(value):
    """Decimal("1234.5") -> "1 234,50 zł" (non-breaking spaces, so it never wraps)."""
    text = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} zł"


@register.filter
def show(value):
    """A field value in a correction's history: amounts in zł, dates as dd/mm/yy."""
    if isinstance(value, Decimal):
        return zl(value)
    if isinstance(value, date):
        return f"{value:%d/%m/%y}"
    return value


@register.filter
def correctable_by(transaction, user):
    return transaction.can_be_corrected_by(user)
