from datetime import date, datetime
from decimal import Decimal

from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()


@register.filter
def zl(value):
    """Decimal("1234.5") -> "1 234,50 zł" (non-breaking spaces, so it never wraps)."""
    text = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} zł"


@register.filter
def show(value):
    """A field value in a correction's history: amounts in zł, dates as dd.mm.yyyy."""
    if isinstance(value, Decimal):
        return zl(value)
    if isinstance(value, date):
        return f"{value:%d.%m.%Y}"
    return value


@register.filter
def day(value):
    """A date – or the day of a datetime, in local time – as dd.mm.yyyy, marked up so that software
    reads it without guessing the order: <time datetime="2026-11-20">20.11.2026</time>."""
    if not value:
        return ""
    if isinstance(value, datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return format_html('<time datetime="{}">{}</time>', value.isoformat(), f"{value:%d.%m.%Y}")


@register.filter
def correctable_by(transaction, user):
    return transaction.can_be_corrected_by(user)
