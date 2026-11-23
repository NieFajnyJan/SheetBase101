"""Splitting an amount equally between the people who chip in. Everyone except the person
owed the money gets a planned transaction for their share; that person's own share isn't created,
because nobody owes themselves. The shares always add up to exactly the amount split. When it doesn't
divide evenly, the leftover grosze go to people drawn by lot, so it's a different person each time."""

import random
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from .models import AlreadyAdded, PlannedTransaction, Transaction, User

GROSZ = Decimal("0.01")


def divide(amount: Decimal, people: int, seed: uuid.UUID) -> list[Decimal]:
    """`amount` split into `people` shares that add up to it exactly. They differ by at most one grosz;
    the seed decides who gets the extra grosze, so the same seed always gives the same shares."""
    grosze = int(amount / GROSZ)
    base, extra = divmod(grosze, people)
    lucky = set(random.Random(seed.int).sample(range(people), extra))
    return [(base + (i in lucky)) * GROSZ for i in range(people)]


@dataclass
class Share:
    member: User
    amount: Decimal


@dataclass
class SplitPlan:
    """What a split would create. `shares` has everyone taking part, the creditor too (if ticked);
    `owed` is just the shares that become planned transactions."""

    creditor: User
    amount: Decimal
    description: str
    seed: uuid.UUID  # becomes the shares' group, so they're deleted together and can't be added twice
    tag: str = ""
    shares: list[Share] = field(default_factory=list)

    @property
    def owed(self):
        return [s for s in self.shares if s.member != self.creditor]

    @property
    def owed_total(self):
        return sum((s.amount for s in self.owed), Decimal("0.00"))

    @property
    def lowest(self):
        return min(s.amount for s in self.shares)

    @property
    def highest(self):
        return max(s.amount for s in self.shares)


def plan_split(creditor, amount, participants, description, seed, tag="") -> SplitPlan:
    """Splits `amount` equally between `participants`, owed to `creditor`. The shares are listed in the
    order given, but drawn in a fixed order (by account), so renaming someone can't change them."""
    participants = list(participants)
    drawn = dict(zip(sorted(participants, key=lambda m: m.pk), divide(amount, len(participants), seed)))
    plan = SplitPlan(creditor=creditor, amount=amount, description=description, seed=seed, tag=tag)
    plan.shares = [Share(m, drawn[m]) for m in participants]
    return plan


def save_split(plan: SplitPlan, created_by, purchase: Transaction | None = None) -> list[PlannedTransaction]:
    """Saves the purchase (if any, unsaved) and one planned transaction per share owed, all at once
    or not at all. The shares are grouped and point to the purchase."""
    with db_transaction.atomic():
        if PlannedTransaction.objects.filter(group=plan.seed).exists():
            raise AlreadyAdded
        if purchase is not None:
            purchase.save()
        return [
            PlannedTransaction.objects.create(
                debtor=share.member, creditor=plan.creditor, amount=share.amount, description=plan.description,
                tag=plan.tag, group=plan.seed, source=purchase, created_by=created_by)
            for share in plan.owed
        ]


def check_can_cancel_shares(purchase: Transaction):
    """Cancelling a split purchase deletes its shares – impossible once someone has paid theirs."""
    paid = purchase.split_shares.settled().select_related("debtor").first()
    if paid:
        raise ValidationError(f"{paid.debtor} has already paid their share of this purchase. "
                              "Cancel that payment first.")


def cancel_shares(purchase: Transaction):
    """The purchase was cancelled, so the shares owed for it go too."""
    with db_transaction.atomic():
        shares = purchase.split_shares.select_for_update()
        check_can_cancel_shares(purchase)
        shares.delete()
