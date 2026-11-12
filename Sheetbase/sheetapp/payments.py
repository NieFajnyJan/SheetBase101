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
    """What a payment would do. `debts` are all the payer's open debts to the person paid (oldest first),
    `chosen` the ones the payment is for; of those, `covered` get settled, and the leftover, if any, is created."""

    debts: list[PlannedTransaction] = field(default_factory=list)
    chosen: set[int] = field(default_factory=set)
    covered: list[PlannedTransaction] = field(default_factory=list)
    leftover: Leftover | None = None

    def signature(self):
        """Identifies the plan, so Confirm can check the debts haven't changed since the screen was shown."""
        left = self.leftover
        return ",".join(str(d.pk) for d in self.covered) + (
            f"|{left.debtor.pk}>{left.creditor.pk}:{left.amount}" if left else "|")

    def rows(self):
        """For the confirmation screen: each open debt, whether it's ticked, and what happens to it:
        "paid", "partly" (paid, but a rest stays owed), "ran_out" (ticked but no money left) or ""."""
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


def default_choice(debts, amount):
    """Which debts a payment is for when nobody picked: a debt of exactly that amount if there is one
    (the oldest such), otherwise oldest first until the money runs out."""
    exact = next((d for d in debts if d.amount == amount), None)
    if exact:
        return {exact.pk}
    chosen, remaining = set(), amount
    for debt in debts:
        if remaining <= 0:
            break
        chosen.add(debt.pk)
        remaining -= debt.amount
    return chosen


def plan_payment(payer, recipient, amount, description, chosen=None, lock=False) -> PaymentPlan:
    """Matches a payment of `amount` from payer to recipient against payer's open debts to recipient.
    `chosen`: ids of the debts it's for (others are ignored); None picks them automatically."""
    debts = (PlannedTransaction.objects.open().filter(debtor=payer, creditor=recipient)
             .order_by("created_at", "pk"))
    if lock:
        debts = debts.select_for_update()
    plan = PaymentPlan(debts=list(debts))
    plan.chosen = default_choice(plan.debts, amount) if chosen is None else {d.pk for d in plan.debts if d.pk in chosen}
    remaining = amount
    for debt in plan.debts:
        if debt.pk not in plan.chosen:
            continue
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
        # Overpaid (or paying for nothing owed): the person paid now owes the payer the difference.
        what = f"{description} – overpaid" if plan.covered else description
        plan.leftover = Leftover(debtor=recipient, creditor=payer, amount=remaining, description=_clip(what))
    return plan


class PlanChanged(Exception):
    """The debts changed between showing the confirmation screen and clicking Confirm."""

    def __init__(self, plan):
        super().__init__("The debts changed in the meantime.")
        self.plan = plan


def record_payment(payment: Transaction, chosen=None, expected_signature=None) -> PaymentPlan:
    """Saves an unsaved payment between members and settles the chosen debts (None: picked
    automatically), all at once or not at all."""
    with db_transaction.atomic():
        plan = plan_payment(payment.payer, payment.recipient, payment.amount, payment.description,
                            chosen=chosen, lock=True)
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
