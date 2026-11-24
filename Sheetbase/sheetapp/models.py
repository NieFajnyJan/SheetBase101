import json
import uuid
from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import IntegrityError, models, transaction as db_transaction
from django.db.models import Case, F, OuterRef, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.functional import cached_property

from audit.querysets import AuditedQuerySet

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


def needs_matching(payer, recipient):
    """A payment between two members pays debts; a purchase or return (to or from a shop) never does."""
    return not (payer.is_shop or recipient.is_shop)


def check_parties(transaction):
    """A done transaction (or a correction of one) is a purchase (member → shop), a return (shop → member)
    or a payment between members. Between two shops it would be none of those, and no summary would count it."""
    if transaction.payer_id and transaction.recipient_id and transaction.payer.is_shop and transaction.recipient.is_shop:
        raise ValidationError({"recipient": "One side has to be a member: a purchase is paid by a member to a shop, "
                                            "a return by a shop to a member. A payment between two shops can't "
                                            "be recorded."})


class AlreadyAdded(Exception):
    """This was saved already: the same form was sent twice (e.g. Add or Confirm clicked twice).
    `record` is what the first one added, when known."""

    def __init__(self, record=None):
        super().__init__("This was already added.")
        self.record = record


def save_once(model, submission, save, also=()):
    """Runs save(), which adds a `model` record from the add form with this submission id, unless
    that form was sent before: then raises AlreadyAdded and saves nothing. The id is unique in the
    database, so the second one fails to save, also when both arrive at the same moment; only then is
    the first one looked up. `also`: other exceptions save() raises when it was sent before (e.g. the
    debts a payment was for are already paid – by the first one). No id (None): just saves."""
    try:
        with db_transaction.atomic():
            return save()
    except (IntegrityError, *also):
        if submission and (first := model.objects.filter(submission=submission).first()):
            raise AlreadyAdded(first) from None
        raise


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
# Once a payment has settled debts, these can't be corrected any more (description and date still can).
MONEY_FIELDS = ["amount", "payer_id", "recipient_id"]


class TransactionQuerySet(AuditedQuerySet):
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
    # The add form it came from (a hidden id), so sending that form twice can't add it twice. See save_once().
    submission = models.UUIDField(null=True, blank=True, unique=True, editable=False)

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
        return f"{self.date:%d.%m.%Y}: {self.description} – {self.amount} zł ({self.payer} → {self.recipient})"

    def clean(self):
        check_parties(self)

    # A done transaction is final: it can be added, never edited or deleted.
    # The original is version 0 of itself; each correction is the next one (see TransactionCorrection.version).
    version = 0

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
        current = self.current
        return current is not self and current.cancelled

    @property
    def is_corrected(self):
        return len(self.versions) > 1

    def touches_debts(self):
        """Whether this payment settled planned transactions or created a leftover."""
        return self.settled_debts.exists() or self.leftovers.exists()

    def is_loan(self):
        """A payment between members that paid no debts: all of it became a debt the other way."""
        return not self.settled_debts.exists() and self.leftovers.exists()

    def paid_for(self):
        """What this payment paid of each debt (payments.Applied), when listing it tells you something:
        it paid several, only part of one, or one that differs from the payment itself.
        Prefetch "settled_debts" when listing many transactions."""
        from .payments import applied

        spread = applied(self)
        current = self.current
        if len(spread) == 1 and not spread[0].owed and (spread[0].debt.description, spread[0].debt.amount) == (
                current.description, current.amount):
            return []
        return spread

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
    # Which correction of the original this is: 1, 2, ... – the version it replaces plus one. Unique per
    # original, so of two corrections of the same version sent at the same moment only the first is saved.
    version = models.PositiveIntegerField(editable=False)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.UniqueConstraint(fields=["original", "version"], name="correction_version_unique"),
            models.CheckConstraint(
                condition=~models.Q(payer=models.F("recipient")),
                name="correction_payer_is_not_recipient",
                violation_error_message="“Paid by” and “paid to” must be different people.",
            ),
        ]

    def __str__(self):
        what = "Cancelled" if self.cancelled else "Corrected"
        return f"{what}: {self.date:%d.%m.%Y}: {self.description} – {self.amount} zł ({self.payer} → {self.recipient})"

    def clean_fields(self, exclude=None):
        # An old date may stay: correcting the amount of a transaction older than MAX_YEARS_BACK keeps its date.
        if self.original_id and self.date == self.original.current.date:
            exclude = {*(exclude or ()), "date"}
        super().clean_fields(exclude)

    def clean(self):
        if not self.original_id:
            return
        # Imported here because payments imports these models.
        from .payments import check_can_undo

        original = self.original
        # A cancelled transaction stays cancelled – record a new one instead.
        if original.corrections.filter(cancelled=True).exclude(pk=self.pk).exists():
            raise ValidationError("This transaction is cancelled and can't be corrected any more.")
        if self.cancelled:
            from .splits import check_can_cancel_shares

            check_can_undo(original)
            check_can_cancel_shares(original)
            return
        if not (self.payer_id and self.recipient_id):
            return
        # Not when cancelling (above): that's how one between two shops, recorded before this check, goes away.
        check_parties(self)
        if not self.changes_money():
            return
        if original.split_shares.exists():
            raise ValidationError("This purchase was split between people, so its amount, “paid by” and "
                                  "“paid to” can't be changed. Cancel it and add it again instead.")
        if original.is_loan():
            # Paid no debts, so the loan can simply follow the correction – unless it's been paid back.
            check_can_undo(original)
            if not needs_matching(self.payer, self.recipient):
                raise ValidationError("A payment between members can't be corrected into a purchase. "
                                      "Cancel it and add the purchase instead.")
            return
        current = original.current
        if original.touches_debts() or (
                # A purchase corrected into a payment between members would skip paying debts.
                needs_matching(self.payer, self.recipient) and not needs_matching(current.payer, current.recipient)):
            raise ValidationError("This payment pays debts between members, so its amount, “paid by” and "
                                  "“paid to” can't be changed. Cancel it and add it again instead.")

    # Loan fields (see payments.LOAN_COPIES) that keep their own value although this correction changes
    # what they were copied from: chosen on the correction form when the loan was changed separately.
    keep_loan = ()

    def changes_money(self):
        current = self.original.current
        return any(getattr(self, f) != getattr(current, f) for f in MONEY_FIELDS)

    def save(self, *args, **kwargs):
        """Set `version` to the one after the version this corrects, as it was shown (the correction form
        does); left empty, it follows the latest one."""
        if not self._state.adding:
            raise ValueError("A correction can't be edited.")
        if self.version is None:
            self.version = self.original.corrections.count() + 1
        try:
            self._save(*args, **kwargs)
        except IntegrityError:
            if self.original.corrections.filter(version=self.version).exists():
                raise ValidationError("This transaction was corrected by someone else at the same moment, "
                                      "so nothing was saved. Open it again to see what it is now.") from None
            raise

    def _save(self, *args, **kwargs):
        with db_transaction.atomic():
            # Only when something the loan copies changed, so a date fix keeps edits made to the loan itself.
            # A loan already paid back is history: a description fix leaves it as it was
            # (money changes to it are refused in clean()).
            previous = self.original.current
            correcting_loan = not self.cancelled and self.original.is_loan() and (
                self.changes_money()
                or (self.description != previous.description
                    and not self.original.leftovers.settled().exists()))
            super().save(*args, **kwargs)
            if self.cancelled:
                from .payments import undo_payment
                from .splits import cancel_shares

                undo_payment(self.original)
                cancel_shares(self.original)
            elif correcting_loan:
                from .payments import correct_loan

                correct_loan(self.original, self, previous, keep=self.keep_loan)

    def delete(self, *args, **kwargs):
        raise ValueError("A correction can't be deleted.")


class GroupChanged(Exception):
    """The rows a delete would remove aren't the ones the user was shown any more."""


def versions(rows):
    """{pk: version} of these planned transactions: any change to one gives it a new version."""
    return {r.pk: r.version for r in rows}


def rows_signature(rows):
    """Which planned transactions a Delete page shows, in which version – sent with Delete, so it deletes
    exactly what was shown."""
    return json.dumps(sorted(versions(rows).items()), separators=(",", ":"))


def changed_rows(rows, signature):
    """Of these rows, the ones changed since the rows with `signature`, or not among them."""
    try:
        before = dict(json.loads(signature))
    except (ValueError, TypeError):
        return {r.pk for r in rows}
    return {pk for pk, version in versions(rows).items() if before.get(pk) != version}


class PlannedTransactionQuerySet(AuditedQuerySet):
    def open(self):
        """Debts that still count. Use this everywhere debts are added up (balances, who owes whom)."""
        return self.filter(settled_by__isnull=True)

    def settled(self):
        return self.filter(settled_by__isnull=False)

    def with_groups(self):
        """These planned transactions plus every other one in their groups."""
        groups = self.exclude(group__isnull=True).values("group")
        return PlannedTransaction.objects.filter(models.Q(pk__in=self.values("pk")) | models.Q(group__in=groups))

    def make_group(self):
        """Puts these rows into one new group (taking them out of any group they were in)."""
        return self.update(group=uuid.uuid4())

    def delete_with_groups(self, expected=None):
        """Deletes these and everyone else's rows in their groups – all or nothing.
        Refused when anyone in a group has already paid (settled debts are never deleted).
        `expected`: the rows_signature() of the rows the user was shown; when the rows are no longer
        exactly those (one was added to the group, changed or removed), raises GroupChanged instead."""
        with db_transaction.atomic():
            rows = self.with_groups().select_for_update()
            if expected is not None and rows_signature(rows) != expected:
                raise GroupChanged
            paid = rows.settled().select_related("debtor").first()
            if paid:
                raise ValidationError(f"{paid.debtor} already paid “{paid.description}”, so it can't be deleted.")
            return rows.delete()


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
    # Just a label, e.g. "pizza"; it doesn't group anything, so it can be reused.
    tag = models.CharField(max_length=50, blank=True, help_text="Optional label, e.g. “pizza”.")
    # The rows of one shared expense (each person who chipped in has their own row) share a generated id,
    # so deleting one deletes them all. Empty: the row stands alone.
    group = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    created_by = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="created_planned_transactions", editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # The add form it came from (a hidden id), so sending that form twice can't add it twice. See save_once().
    submission = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    # Goes up by one with each change, so a page can tell whether a debt changed since it showed it,
    # and a save from an older version is refused (see save()).
    version = models.PositiveIntegerField(default=0, editable=False)

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
    # Set on a share of a purchase split between people, so cancelling the purchase cancels its shares.
    source = models.ForeignKey(
        Transaction, on_delete=models.PROTECT, null=True, blank=True, editable=False,
        related_name="split_shares", verbose_name="share of purchase",
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

    def can_be_marked_paid_by(self, user):
        """Either side of a debt may record that it was paid – nobody else."""
        return user.pk in (self.debtor_id, self.creditor_id)

    # A settled debt is history: it can't be changed or deleted (only reopened when the payment that settled it is cancelled).
    def save(self, *args, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        with db_transaction.atomic():
            # Only from the version it was loaded in: the version goes up in the same step, so of two
            # changes made at the same moment the second one is refused rather than replacing the first.
            # Paying it counts as a change too. (_base_manager: not logged on its own – the save below logs
            # the whole change.)
            bumped = (PlannedTransaction._base_manager.filter(pk=self.pk, version=self.version)
                      .update(version=F("version") + 1))
            if not bumped:
                raise ValidationError("This debt was changed or paid by someone else at the same moment, so "
                                      "nothing was saved. Open it again to see what it is now.")
            if PlannedTransaction.objects.settled().filter(pk=self.pk).exists():
                raise ValueError("A settled planned transaction can't be edited.")
            self.version += 1
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_settled:
            raise ValueError("A settled planned transaction can't be deleted.")
        return super().delete(*args, **kwargs)
