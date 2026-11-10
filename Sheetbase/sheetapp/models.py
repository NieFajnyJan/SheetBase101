from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Case, F, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.functional import cached_property


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


# The fields a correction can change, in the order they're shown.
CORRECTABLE_FIELDS = ["amount", "payer", "recipient", "description", "date"]


class TransactionQuerySet(models.QuerySet):
    def with_current(self):
        """Annotates each transaction with its *current* values (cur_amount, cur_payer, ...):
        those of its latest correction, or its own when it has none. cur_counted is what it adds
        to a total – 0 once cancelled. Filter, sort and sum on these, never on the original fields."""
        latest = TransactionCorrection.objects.filter(original=OuterRef("pk")).order_by("-created_at", "-pk")

        def current(field, output_field=None):
            return Coalesce(Subquery(latest.values(field)[:1]), F(field), output_field=output_field)

        return self.annotate(
            cur_amount=current("amount"),
            cur_payer=current("payer_id", models.BigIntegerField()),  # a user's pk
            cur_recipient=current("recipient_id", models.BigIntegerField()),
            cur_date=current("date"),
            cur_cancelled=Coalesce(Subquery(latest.values("cancelled")[:1]), Value(False)),
        ).annotate(
            cur_counted=Case(When(cur_cancelled=True, then=Value(Decimal("0.00"))), default=F("cur_amount"),
                             output_field=models.DecimalField(max_digits=10, decimal_places=2)),
        )


class Transaction(models.Model):
    """A done transaction: a real payment that already happened – who paid whom, how much,
    for what and when. It's final; mistakes are fixed with a TransactionCorrection."""

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

    objects = TransactionQuerySet.as_manager()

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

    # --- Corrections. Prefetch "corrections" when listing many transactions.
    @cached_property
    def versions(self):
        """Every version, newest first: the corrections, then this original."""
        return [*self.corrections.all(), self]

    @property
    def current(self):
        """The version that counts: the latest correction, or the original."""
        return self.versions[0]

    @property
    def is_cancelled(self):
        return self.current is not self and self.current.cancelled

    @property
    def is_corrected(self):
        return len(self.versions) > 1

    def can_be_corrected_by(self, user):
        """Only whoever added the transaction can correct or cancel it, and not once it's cancelled."""
        return user.pk == self.created_by_id and not self.is_cancelled

    def history(self):
        """For each correction, newest first: what changed compared with the version before it
        (field label, old, new)."""
        oldest_first = self.versions[::-1]
        entries = []
        for before, after in zip(oldest_first, oldest_first[1:]):
            changes = [
                (TransactionCorrection._meta.get_field(f).verbose_name.removesuffix(" (zł)"),
                 getattr(before, f), getattr(after, f))
                for f in CORRECTABLE_FIELDS if getattr(before, f) != getattr(after, f)
            ]
            entries.append({"correction": after, "changes": changes})
        return entries[::-1]


class TransactionCorrection(models.Model):
    """A new version of a done transaction. The original is never changed; the latest
    correction wins. A cancellation keeps the values it cancels but counts as 0 zł.
    Final like Transaction: it can only be followed by another correction."""

    original = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="corrections")
    amount = models.DecimalField(
        "amount (zł)", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    payer = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", verbose_name="paid by")
    recipient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", verbose_name="paid to")
    description = models.CharField(max_length=255, help_text="What was bought.")
    date = models.DateField("date paid", validators=[validate_transaction_date])
    cancelled = models.BooleanField(default=False)
    reason = models.CharField(max_length=255, help_text="Why it's being corrected.")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(payer=models.F("recipient")),
                name="correction_payer_is_not_recipient",
                violation_error_message="“Paid by” and “paid to” must be different people.",
            ),
        ]

    def __str__(self):
        what = "Cancelled" if self.cancelled else "Corrected"
        return f"{what}: {self.date:%d/%m/%y}: {self.description} – {self.amount} zł ({self.payer} → {self.recipient})"

    def clean(self):
        # A cancelled transaction stays cancelled – record a new one instead.
        if self.original_id and self.original.corrections.filter(cancelled=True).exclude(pk=self.pk).exists():
            raise ValidationError("This transaction is cancelled and can't be corrected any more.")

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("A correction can't be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("A correction can't be deleted.")


class PlannedTransactionQuerySet(models.QuerySet):
    def open(self):
        """Debts that still count. Use this everywhere debts are added up (balances, who owes whom)."""
        return self.filter(settled_by__isnull=True)

    def settled(self):
        return self.filter(settled_by__isnull=False)


class PlannedTransaction(models.Model):
    """A planned transaction: an open debt – who owes whom, how much, for what and since when.
    No money has moved yet. When it's paid it isn't deleted but marked settled and linked to the
    done transaction that paid it, so cancelling that payment can reopen it."""

    # Only members: shops never owe or are owed anything.
    debtor = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="debts", verbose_name="owes",
        limit_choices_to={"is_shop": False},
    )
    creditor = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="credits", verbose_name="owed to",
        limit_choices_to={"is_shop": False},
    )
    amount = models.DecimalField(
        "amount (zł)", max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))]
    )
    description = models.CharField(max_length=255, help_text="What it's for.")
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="created_planned_transactions", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Settling. Null = still open.
    settled_by = models.ForeignKey(
        Transaction, on_delete=models.PROTECT, null=True, blank=True, editable=False,
        related_name="settled_debts", verbose_name="settled by payment",
    )
    settled_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Set on a leftover (over- or underpayment) created by a payment.
    created_by_payment = models.ForeignKey(
        Transaction, on_delete=models.PROTECT, null=True, blank=True, editable=False,
        related_name="leftovers", verbose_name="leftover of payment",
    )

    objects = PlannedTransactionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(debtor=models.F("creditor")),
                name="planned_debtor_is_not_creditor",
                violation_error_message="“Owes” and “owed to” must be different people.",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name="planned_amount_positive",
                violation_error_message="The amount must be above 0.",
            ),
            # Settled means both: which payment and when.
            models.CheckConstraint(
                condition=models.Q(settled_by__isnull=True, settled_at__isnull=True)
                | models.Q(settled_by__isnull=False, settled_at__isnull=False),
                name="planned_settled_by_and_at_together",
            ),
        ]

    def __str__(self):
        return f"{self.debtor} owes {self.creditor} {self.amount} zł – {self.description}"

    @property
    def is_settled(self):
        return self.settled_by_id is not None

    # A settled debt is history: it can't be changed or deleted (only reopened when the payment that settled it is cancelled).
    def save(self, *args, **kwargs):
        if not self._state.adding and PlannedTransaction.objects.settled().filter(pk=self.pk).exists():
            raise ValueError("A settled planned transaction can't be edited.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_settled:
            raise ValueError("A settled planned transaction can't be deleted.")
        return super().delete(*args, **kwargs)
