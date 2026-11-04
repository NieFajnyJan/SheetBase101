from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    """Office member. Builds on Django's user (first_name, last_name, username, password...),
    so login, admin and later Google/Discord login work out of the box."""

    email = models.EmailField("email address", unique=True)

    class Meta:
        ordering = ["first_name", "last_name", "username"]

    def __str__(self):
        return self.get_full_name() or self.username


class Transaction(models.Model):
    """One office expense: who paid, how much, for what and when."""

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
    description = models.CharField(max_length=255, help_text="What was bought.")
    date = models.DateField("date paid", default=timezone.localdate)
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="created_transactions", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.date}: {self.description} – {self.amount} zł ({self.payer})"
