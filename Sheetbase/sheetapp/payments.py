"""Paying debts: a done transaction between two members settles what the payer owes the person
paid, oldest first, and whatever doesn't match exactly becomes a new planned transaction (a leftover).
Purchases and returns (anything to or from a shop) never touch debts."""

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
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


@dataclass
class PaymentPlan:
    """What a payment would do: the debts it settles (oldest first) and the leftover, if any."""

    covered: list[PlannedTransaction] = field(default_factory=list)
    leftover: Leftover | None = None

    def signature(self):
        """Identifies the plan, so Confirm can check the debts haven't changed since the screen was shown."""
        left = self.leftover
        return ",".join(str(d.pk) for d in self.covered) + (
            f"|{left.debtor.pk}>{left.creditor.pk}:{left.amount}" if left else "|")


def _clip(text):
    return text[:DESCRIPTION_MAX]


def plan_payment(payer, recipient, amount, description, lock=False) -> PaymentPlan:
    """Matches a payment of `amount` from payer to recipient against payer's open debts to recipient."""
    debts = (PlannedTransaction.objects.open().filter(debtor=payer, creditor=recipient)
             .order_by("created_at", "pk"))
    if lock:
        debts = debts.select_for_update()
    plan = PaymentPlan()
    remaining = amount
    for debt in debts:
        if remaining <= 0:
            break
        plan.covered.append(debt)
        if debt.amount > remaining:
            # Underpaid: this debt is settled anyway and the payer still owes the rest.
            plan.leftover = Leftover(debtor=payer, creditor=recipient, amount=debt.amount - remaining,
                                     description=_clip(f"{debt.description} – rest"), tag=debt.tag)
            return plan
        remaining -= debt.amount
    if remaining > 0:
        # Overpaid (or nothing owed at all): the person paid now owes the payer the difference.
        what = f"{description} – overpaid" if plan.covered else description
        plan.leftover = Leftover(debtor=recipient, creditor=payer, amount=remaining, description=_clip(what))
    return plan


class PlanChanged(Exception):
    """The debts changed between showing the confirmation screen and clicking Confirm."""

    def __init__(self, plan):
        super().__init__("The debts changed in the meantime.")
        self.plan = plan


def record_payment(payment: Transaction, expected_signature=None) -> PaymentPlan:
    """Saves an unsaved payment between members and settles its debts, all at once or not at all."""
    with db_transaction.atomic():
        plan = plan_payment(payment.payer, payment.recipient, payment.amount, payment.description, lock=True)
        if expected_signature is not None and plan.signature() != expected_signature:
            raise PlanChanged(plan)
        payment.save()
        PlannedTransaction.objects.filter(pk__in=[d.pk for d in plan.covered]).update(
            settled_by=payment, settled_at=timezone.now())
        if left := plan.leftover:
            PlannedTransaction.objects.create(
                debtor=left.debtor, creditor=left.creditor, amount=left.amount, description=left.description,
                tag=left.tag, created_by=payment.created_by, created_by_payment=payment)
    return plan


def check_can_undo(payment: Transaction):
    """Cancelling removes the leftover a payment created – impossible once that leftover is paid."""
    if payment.leftovers.settled().exists():
        raise ValidationError("A debt left over from this payment has already been paid. "
                              "Cancel that later payment first.")


def undo_payment(payment: Transaction):
    """When a payment is cancelled: removes the leftover it created and reopens the debts it settled."""
    with db_transaction.atomic():
        leftovers = payment.leftovers.select_for_update()
        check_can_undo(payment)
        leftovers.delete()
        payment.settled_debts.update(settled_by=None, settled_at=None)
