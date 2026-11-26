"""Paying debts: a done transaction between two members settles what the payer owes the person
paid – oldest first (Mark paid: the debt clicked first) – and whatever doesn't match
exactly becomes a new planned transaction (a leftover).
Purchases and returns (anything to or from a shop) never touch debts."""

import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db import models
from django.db.models import F
from django.utils import timezone

from .models import PlannedTransaction, Transaction, User

DESCRIPTION_MAX = PlannedTransaction._meta.get_field("description").max_length


@dataclass
class Leftover:
    debtor: User
    creditor: User
    amount: Decimal
    description: str
    tag: str = ""
    # The rest of a partly paid debt keeps the purchase it came from and its group: it's still part of
    # that shared expense, so it can't be deleted on its own once someone in the group has paid.
    source_id: int | None = None
    group: uuid.UUID | None = None


@dataclass
class PaymentPlan:
    """What a payment would do. `debts` are all the payer's open debts to the person paid, in the order it
    pays them; `chosen` the ones the payment is for; of those, `covered` get settled, and the leftover, if any,
    is created."""

    debts: list[PlannedTransaction] = field(default_factory=list)
    chosen: set[int] = field(default_factory=set)
    covered: list[PlannedTransaction] = field(default_factory=list)
    leftover: Leftover | None = None

    def reviewed(self):
        """What Confirm commits to, as the confirmation screen shows it: which debts it pays, each in
        its version (see PlannedTransaction.version), and the leftover."""
        left = self.leftover
        return {
            "paid": [[d.pk, d.version] for d in self.covered],
            "leftover": [left.debtor.pk, left.creditor.pk, str(left.amount), left.description] if left else None,
        }

    def signature(self):
        """The plan as reviewed, sent with Confirm: saving checks the plan still says exactly this,
        so a debt that was changed, added or removed in the meantime is shown again first."""
        return json.dumps(self.reviewed(), ensure_ascii=False, separators=(",", ":"))

    def changes_since(self, signature):
        """Compared with the plan that had `signature`: the debts paid differently now (newly paid, no
        longer paid, or changed), and whether the leftover differs."""
        try:
            before = json.loads(signature)
            paid_before, leftover_before = dict(before["paid"]), before["leftover"]
        except (ValueError, KeyError, TypeError):
            return {d.pk for d in self.debts}, True  # can't tell: check it all
        now = self.reviewed()
        paid_now = dict(now["paid"])
        return {pk for pk, _ in paid_before.items() ^ paid_now.items()}, leftover_before != now["leftover"]

    def rows(self):
        """For the confirmation screen: each open debt, whether the payment is for it, and what happens to it:
        "paid", "partly" (paid, but a rest stays owed), "ran_out" (for it, but no money left) or ""."""
        covered = {d.pk for d in self.covered}
        last = self.covered[-1] if self.covered else None
        # Underpaid: the leftover is owed by the payer, for the rest of the last covered debt.
        partly = last.pk if last and self.leftover and self.leftover.debtor.pk == last.debtor_id else None
        for debt in self.debts:
            if debt.pk == partly:
                status = "partly"
            elif debt.pk in covered:
                status = "paid"
            elif debt.pk in self.chosen:
                status = "ran_out"
            else:
                status = ""
            yield debt, debt.pk in self.chosen, status


def _clip(text):
    return text[:DESCRIPTION_MAX]


def plan_payment(payer, recipient, amount, description, chosen=None, first=None, lock=False) -> PaymentPlan:
    """Matches a payment of `amount` from payer to recipient against payer's open debts to recipient,
    as far as the money goes. `first`: the id of a debt to pay before the others (Mark paid on it).
    `chosen`: ids of the only debts it's for (the admin's “Mark as paid”); None: all of them."""
    # Oldest first; debts added at the same moment go in the order they were saved, so the same
    # payment always picks the same debts.
    debts = (PlannedTransaction.objects.open().filter(debtor=payer, creditor=recipient)
             .order_by("created_at", "pk"))
    if lock:
        debts = debts.select_for_update()
    debts = sorted(debts, key=lambda debt: debt.pk != first)  # stable: the rest stay oldest first
    plan = PaymentPlan(debts=debts)
    if chosen is not None:
        plan.chosen = {debt.pk for debt in plan.debts if debt.pk in chosen}
    remaining = amount
    for debt in plan.debts:
        if remaining <= 0:
            break
        if chosen is None:
            plan.chosen.add(debt.pk)
        elif debt.pk not in plan.chosen:
            continue
        plan.covered.append(debt)
        if debt.amount > remaining:
            # Underpaid: this debt is settled anyway and the payer still owes the rest.
            plan.leftover = Leftover(debtor=payer, creditor=recipient, amount=debt.amount - remaining,
                                     description=_clip(f"{debt.description} – rest"), tag=debt.tag,
                                     source_id=debt.source_id, group=debt.group)
            return plan
        remaining -= debt.amount
    if remaining > 0:
        # Overpaid (or paying for nothing owed): the person paid now owes the payer the difference.
        what = f"{description} – overpaid" if plan.covered else description
        plan.leftover = Leftover(debtor=recipient, creditor=payer, amount=remaining, description=_clip(what))
    return plan


class PlanChanged(Exception):
    """The debts changed between showing the confirmation screen and clicking Confirm."""

    def __init__(self, plan):
        super().__init__("The debts changed in the meantime.")
        self.plan = plan


def record_payment(payment: Transaction, chosen=None, first=None, expected_signature=None) -> PaymentPlan:
    """Saves an unsaved payment between members and settles the debts it pays (see plan_payment),
    all at once or not at all."""
    with db_transaction.atomic():
        plan = plan_payment(payment.payer, payment.recipient, payment.amount, payment.description,
                            chosen=chosen, first=first, lock=True)
        if expected_signature is not None and plan.signature() != expected_signature:
            raise PlanChanged(plan)
        payment.save()
        PlannedTransaction.objects.filter(pk__in=[d.pk for d in plan.covered]).update(
            settled_by=payment, settled_at=timezone.now(), version=F("version") + 1)
        if left := plan.leftover:
            PlannedTransaction.objects.create(
                debtor=left.debtor, creditor=left.creditor, amount=left.amount, description=left.description,
                tag=left.tag, source_id=left.source_id, group=left.group, created_by=payment.created_by,
                created_by_payment=payment)
    return plan


def mark_paid(debts, created_by):
    """Records that these open debts were paid: one payment per debtor → creditor pair, for exactly
    what that pair's debts add up to, settling them. All at once or not at all. Returns the payments."""
    pairs = {}
    for debt in debts.select_related("debtor", "creditor").order_by("created_at", "pk"):
        pairs.setdefault((debt.debtor, debt.creditor), []).append(debt)
    payments = []
    with db_transaction.atomic():
        for (debtor, creditor), rows in pairs.items():
            payment = Transaction(payer=debtor, recipient=creditor, amount=sum(d.amount for d in rows),
                                  description=_clip(", ".join(d.description for d in rows)), created_by=created_by)
            record_payment(payment, chosen={d.pk for d in rows})
            payments.append(payment)
    return payments


@dataclass
class Applied:
    """What a payment did for one debt: `paid` of its `debt.amount`; `owed` stays owed (a partly paid debt)."""
    debt: PlannedTransaction
    paid: Decimal

    @property
    def owed(self):
        return self.debt.amount - self.paid


def applied(payment: Transaction):
    """How a payment was spread over the debts it paid, oldest first: all of each, except the last one
    when the money ran out (a debt is paid at most partly, never past its amount). Exact, because neither
    a payment that paid debts nor a paid debt can change its amount any more."""
    remaining = payment.amount
    result = []
    for debt in sorted(payment.settled_debts.all(), key=lambda d: (d.created_at, d.pk)):
        paid = min(debt.amount, remaining)
        remaining -= paid
        result.append(Applied(debt, paid))
    return result


@dataclass
class Blocker:
    """A debt this transaction made that was paid since, so cancelling it would undo that payment too."""
    debt: PlannedTransaction
    payment: Transaction


@dataclass
class CancelEffects:
    """What cancelling a done transaction would do to the debts."""
    reopened: list[PlannedTransaction] = field(default_factory=list)  # debts it paid: owed again
    removed: list[PlannedTransaction] = field(default_factory=list)  # its leftovers and split shares
    blockers: list[Blocker] = field(default_factory=list)  # why it can't be cancelled, if it can't


def cancel_effects(transaction: Transaction) -> CancelEffects:
    debts = PlannedTransaction.objects.select_related("debtor", "creditor", "settled_by__created_by")
    effects = CancelEffects(
        reopened=list(debts.filter(settled_by=transaction).order_by("created_at", "pk")),
        removed=list(debts.filter(models.Q(created_by_payment=transaction) | models.Q(source=transaction))
                     .order_by("created_at", "pk")))
    effects.blockers = [Blocker(d, d.settled_by) for d in effects.removed if d.is_settled]
    return effects


def check_can_undo(payment: Transaction):
    """Cancelling removes the leftover a payment created – impossible once that leftover is paid."""
    if payment.leftovers.settled().exists():
        raise ValidationError("A debt left over from this payment has already been paid. "
                              "Cancel that later payment first.")


# What a loan copies from the payment that made it: (the loan's field, the payment's).
LOAN_COPIES = [("debtor_id", "recipient_id"), ("creditor_id", "payer_id"), ("amount", "amount"),
               ("description", "description")]


def loan_value(version, field):
    """What the loan gets from this version of the payment for `field` (one of the payment's in LOAN_COPIES)."""
    value = getattr(version, field)
    return _clip(value) if field == "description" else value


def loan_edits(payment: Transaction):
    """The open loan a payment made, and what was changed on the loan itself since (on the Planned page):
    {loan field: payment field} for the fields where it no longer says what the payment says.
    (None, {}) when the payment made no loan, or it's been paid back."""
    loan = payment.leftovers.open().select_related("debtor", "creditor").first()
    if loan is None or payment.settled_debts.exists():
        return None, {}
    current = payment.current
    return loan, {lf: pf for lf, pf in LOAN_COPIES if getattr(loan, lf) != loan_value(current, pf)}


def correct_loan(payment: Transaction, correction, previous, keep=()):
    """A payment that only made a loan was corrected (`previous` is the version before): the loan follows
    what the correction changed – the amount, people or description – except the loan fields in `keep`.
    What wasn't corrected stays as it is on the loan, also when it was changed there."""
    loan = payment.leftovers.select_for_update().get()
    if loan.is_settled:
        raise ValidationError("This loan has already been paid back, so the payment can't be corrected.")
    for loan_field, field in LOAN_COPIES:
        if loan_field not in keep and getattr(correction, field) != getattr(previous, field):
            setattr(loan, loan_field, loan_value(correction, field))
    loan.save()


def undo_payment(payment: Transaction):
    """When a payment is cancelled: removes the leftover it created and reopens the debts it settled."""
    with db_transaction.atomic():
        leftovers = payment.leftovers.select_for_update()
        check_can_undo(payment)
        leftovers.delete()
        payment.settled_debts.update(settled_by=None, settled_at=None, version=F("version") + 1)
