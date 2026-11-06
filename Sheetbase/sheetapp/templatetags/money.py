from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def zl(value):
    """Decimal("1234.5") -> "1 234,50 zł" (non-breaking spaces, so it never wraps)."""
    text = f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")
    return f"{text} zł"
