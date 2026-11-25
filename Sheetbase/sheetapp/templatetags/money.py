from datetime import date, datetime
from decimal import Decimal

from django import template
from django.utils import timezone
from django.utils.html import format_html

register = template.Library()


def amount_text(value, space="\u00a0"):
    """Decimal("-1234.5") -> "−1 234,50": grouped thousands, a decimal comma and a real minus sign."""
    return f"{Decimal(value):,.2f}".replace(",", space).replace(".", ",").replace("-", "\u2212")


@register.filter
def zl(value):
    """Decimal("1234.5") -> "1 234,50 zł" (non-breaking spaces, so it never wraps)."""
    return f"{amount_text(value)}\u00a0zł"


def party_label(user):
    """A user's name in plain text (a dropdown option): shops marked, so they aren't taken for people."""
    return f"{user} (shop)" if user.is_shop else str(user)


@register.filter
def party(user):
    """Who paid or was paid: the name, with a “shop” pill next to a shop's."""
    if getattr(user, "is_shop", False):
        return format_html('{} <span class="tag tag-shop">shop</span>', user)
    return user


@register.filter
def show(value):
    """A field value in a correction's history: amounts in zł, dates as dd.mm.yyyy, shops marked."""
    if getattr(value, "is_shop", False):
        return party(value)
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
