"""Shops – users with is_shop=True: their names, renaming one, and merging one into another.
Merging is how a shop goes away (a duplicate, or one added with a typo): its transactions move to the other
shop first, so none is lost and every total stays the same."""

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction
from django.db.models import F, Func, OuterRef, Q, Subquery
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify

from .models import Transaction, TransactionCorrection, User


def check_name(name, shop=None):
    """The username a shop called `name` gets; refused when another shop has that name already.
    `shop`: the one being renamed (it may keep its own name, or change only its capitals)."""
    username = "shop-" + slugify(name, allow_unicode=True)
    if username == "shop-":
        raise ValidationError("Enter a name with letters or digits.")
    others = User.objects.exclude(pk=shop.pk) if shop else User.objects.all()
    if (others.filter(Q(username=username) | Q(email=f"{username}@sheetbase.invalid")).exists()
            or others.filter(is_shop=True, first_name__iexact=name).exists()):
        raise ValidationError("This shop already exists.")
    return username


def add(name):
    username = check_name(name)
    shop = User(username=username, email=f"{username}@sheetbase.invalid", first_name=name, is_shop=True)
    shop.set_unusable_password()
    shop.save()
    return shop


def rename(shop, name):
    """The new name everywhere: the shop's transactions show whatever it's called now."""
    username = check_name(name, shop)
    shop.first_name, shop.last_name = name, ""
    shop.username, shop.email = username, f"{username}@sheetbase.invalid"
    shop.save()


def involving(side):
    """Done transactions whose current version has the shop (OuterRef `side`) as payer or recipient."""
    return Transaction.objects.with_current().filter(Q(cur_payer=OuterRef(side)) | Q(cur_recipient=OuterRef(side)))


def with_counts():
    """Every shop, by name, with how many done transactions it's in now (cancelled ones too) – one query."""
    count = involving("pk").order_by().values(n=Func(F("pk"), function="COUNT"))
    return User.objects.filter(is_shop=True).annotate(transactions=Subquery(count))


def merge(shop, into):
    """Moves everything of `shop` to the shop `into`, then deletes `shop` – all or nothing. Every version
    of a transaction moves (originals and corrections), so its history stays as it was apart from the
    name. Refused when the two paid each other: that would become a shop paying itself.
    Returns how many done transactions moved."""
    with db_transaction.atomic():
        # Locked, so nothing is added to the shop while it's being merged away.
        locked = {u.pk: u for u in User.objects.select_for_update().filter(pk__in=[shop.pk, into.pk], is_shop=True)}
        if shop.pk == into.pk or len(locked) != 2:
            raise ValidationError("Pick another shop to merge into.")
        between = Q(payer=shop, recipient=into) | Q(payer=into, recipient=shop)
        if Transaction.objects.filter(between).exists() or TransactionCorrection.objects.filter(between).exists():
            # Even a cancelled one: it keeps its payer and recipient, and paying yourself can't be stored.
            raise ValidationError(f"{shop} and {into} paid each other, so they can't be merged – "
                                  f"that transaction would become a shop paying itself.")
        moved = Transaction.objects.filter(
            Q(payer=shop) | Q(recipient=shop) | Q(corrections__payer=shop) | Q(corrections__recipient=shop)
        ).distinct().count()
        # Logged in the audit log row by row, with each before and after (AuditedQuerySet.update).
        for model in (Transaction, TransactionCorrection):
            model.objects.filter(payer=shop).update(payer=into)
            model.objects.filter(recipient=shop).update(recipient=into)
        try:
            locked[shop.pk].delete()
        except ProtectedError:  # something else still points at it (shops never owe, so it shouldn't)
            raise ValidationError(f"{shop} is still used somewhere else, so it can't be deleted.") from None
    return moved
