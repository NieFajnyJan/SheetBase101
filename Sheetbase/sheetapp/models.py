from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


# Shops are users with is_shop=True that can't log in; purchases are paid to them.
# This one is created in migration 0003; more can be added on the Transactions page.
SHOP_USERNAME = "shop"

# How far back a transaction may be logged.
MAX_YEARS_BACK = 2


def earliest_allowed_date(today):
    try:
        return today.replace(year=today.year - MAX_YEARS_BACK)
    except ValueError:  # today is 29 Feb
        return today.replace(year=today.year - MAX_YEARS_BACK, day=28)


def validate_transaction_date(value):
    """Not in the future, and at most MAX_YEARS_BACK years ago."""
    today = timezone.localdate()
    if value > today:
        raise ValidationError("The date can't be in the future.")
    if value < earliest_allowed_date(today):
        raise ValidationError(f"The date can be at most {MAX_YEARS_BACK} years back.")


class User(AbstractUser):
    """Office member. Builds on Django's user (first_name, last_name, username, password...),
    so login, admin and later Google/Discord login work out of the box."""

    email = models.EmailField("email address", unique=True)
    is_shop = models.BooleanField("shop", default=False, help_text="A shop, not a person. Can't log in.")

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.get_full_name() or self.username


class Transaction(models.Model):
    """A done transaction: a real payment that already happened – who paid whom, how much,
    for what and when."""

    # DecimalField, never float: amounts must be exact to the grosz.
    amount = models.DecimalField(
        "amount (zł)",
        max_digits=10,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    # PROTECT: a user who has paid for something can't be deleted and take the money history with them.
    payer = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="paid_transactions", verbose_name="paid by"
    )
    # Always a user – a purchase is paid to a shop (a user with is_shop=True).
    recipient = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="received_transactions", verbose_name="paid to"
    )
    description = models.CharField(max_length=255, help_text="What was bought.")
    date = models.DateField("date paid", default=timezone.localdate, validators=[validate_transaction_date])
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="created_transactions", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]
        constraints = [
            # Paying yourself isn't a real transaction.
            models.CheckConstraint(
                condition=~models.Q(payer=models.F("recipient")),
                name="transaction_payer_is_not_recipient",
                violation_error_message="“Paid by” and “paid to” must be different people.",
            ),
        ]

    def __str__(self):
        return f"{self.date:%d/%m/%y}: {self.description} – {self.amount} zł ({self.payer} → {self.recipient})"

    # A done transaction is final: it can be added, never edited or deleted.
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("A done transaction can't be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("A done transaction can't be deleted.")
