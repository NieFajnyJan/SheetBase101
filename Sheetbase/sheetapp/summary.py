"""Summaries: per member, what they spent and paid – for one year, or all time.
Done transactions only, with their corrected values; cancelled ones don't count."""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Case, DecimalField, Exists, F, Max, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, ExtractMonth
from django.utils import timezone

from .models import Transaction, User

ZERO = Decimal("0.00")
AMOUNT = DecimalField(max_digits=12, decimal_places=2)
# In English whatever language the server runs in (strftime("%B") would follow it).
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December"]


@dataclass
class MemberRow:
    member: User
    purchases: Decimal = ZERO  # paid to shops
    returns: Decimal = ZERO  # paid back by shops
    paid: Decimal = ZERO  # paid to other members
    received: Decimal = ZERO  # received from other members
    first_date: object = None  # date of their first transaction (all-time summary)
    months: int = 0  # complete months from their first transaction to the latest one in the app (all-time)

    @property
    def total_spent(self):
        """Net out of pocket: what it cost them after shops and members paid them back."""
        return self.purchases - self.returns + self.paid - self.received

    @property
    def per_month(self):
        """Like the spreadsheet: =ROUND(ABS(saldo netto) / DATEDIF(first; latest; "M"); 2).
        Saldo netto here is total_spent – once planned transactions exist, use the real balance.
        None while there's less than one complete month (Excel would show #DIV/0!)."""
        if not self.months:
            return None
        return (abs(self.total_spent) / self.months).quantize(ZERO, ROUND_HALF_UP)


@dataclass
class ShopRow:
    shop: User
    sold: Decimal = ZERO  # members bought from it
    returned: Decimal = ZERO  # it paid back to members


@dataclass
class Summary:
    year: int = None  # None = all time
    rows: list = field(default_factory=list)
    shop_rows: list = field(default_factory=list)  # only shops used in the period
    purchases: Decimal = ZERO
    returns: Decimal = ZERO
    paid: Decimal = ZERO
    received: Decimal = ZERO
    total: Decimal = ZERO  # every transaction in the period – equals the history with the same filter
    # Paid between two shops, recorded before that was refused: neither a purchase nor a return, so in no row.
    between_shops: Decimal = ZERO

    @property
    def total_spent(self):
        # Payments between members cancel out overall, so this is purchases − returns.
        return self.purchases - self.returns + self.paid - self.received


def complete_months(start, end):
    """Complete months from start to end, like Excel's DATEDIF(start; end; "M"):
    7 Oct → 6 Nov = 0, 7 Oct → 7 Nov = 1, 31 Jan → 28 Feb = 0."""
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def with_kinds(transactions):
    """The done transactions that count (not cancelled), each with whether its payer and its recipient
    (as they are now) are shops – which makes it a purchase, a return, or a payment between members."""
    def is_shop(side):
        return Exists(User.objects.filter(pk=OuterRef(side), is_shop=True))

    return transactions.filter(cur_cancelled=False).annotate(payer_shop=is_shop("cur_payer"),
                                                             recipient_shop=is_shop("cur_recipient"))


def summarise(transactions, year=None, count_months=False):
    done = with_kinds(transactions)

    def total(side, **kind):
        """What everyone moved on `side` ("cur_payer": paid, "cur_recipient": received) of `kind`."""
        per_user = (done.filter(**{side: OuterRef("pk")}, **kind).order_by().values(side)
                    .annotate(amount=Sum("cur_amount")).values("amount"))
        return Coalesce(Subquery(per_user), Value(ZERO), output_field=AMOUNT)

    # One query, a row per user. A member's “paid” and “received” are with other members; a shop's are
    # what it paid back (returned) and what members paid it (sold).
    users = User.objects.annotate(
        purchases=total("cur_payer", recipient_shop=True),
        returns=total("cur_recipient", payer_shop=True),
        paid=total("cur_payer", recipient_shop=False),
        received=total("cur_recipient", payer_shop=False),
        first=Subquery(done.filter(Q(cur_payer=OuterRef("pk")) | Q(cur_recipient=OuterRef("pk")))
                       .order_by("cur_date").values("cur_date")[:1]),
    )
    # And one for the whole period.
    period = done.aggregate(
        total=Sum("cur_amount", default=ZERO), latest=Max("cur_date"),
        between_shops=Sum("cur_amount", filter=Q(payer_shop=True, recipient_shop=True), default=ZERO))

    # SQLite sums decimals with a long tail (192.1000000000000) – round to whole grosze.
    summary = Summary(year, total=period["total"].quantize(ZERO),
                      between_shops=period["between_shops"].quantize(ZERO))
    for user in users:  # User ordering: by name
        if user.is_shop:
            if user.received or user.paid:
                summary.shop_rows.append(ShopRow(user, sold=user.received.quantize(ZERO),
                                                 returned=user.paid.quantize(ZERO)))
            continue
        row = MemberRow(user, purchases=user.purchases.quantize(ZERO), returns=user.returns.quantize(ZERO),
                        paid=user.paid.quantize(ZERO), received=user.received.quantize(ZERO),
                        first_date=user.first)
        if count_months and row.first_date:
            row.months = complete_months(row.first_date, period["latest"])
        summary.rows.append(row)
    for column in ("purchases", "returns", "paid", "received"):
        setattr(summary, column, sum((getattr(r, column) for r in summary.rows), ZERO))
    return summary


def monthly_spent(year):
    """What members spent on shops in each month of `year` – purchases minus returns, as Total spent
    overall (payments between members cancel out): 12 amounts, January first."""
    spent = Case(When(payer_shop=False, recipient_shop=True, then=F("cur_amount")),
                 When(payer_shop=True, recipient_shop=False, then=-F("cur_amount")),
                 default=Value(ZERO), output_field=AMOUNT)
    months = (with_kinds(Transaction.objects.with_current().filter(cur_date__year=year)).order_by()
              .annotate(month=ExtractMonth("cur_date")).values("month").annotate(spent=Sum(spent)))
    by_month = {row["month"]: row["spent"].quantize(ZERO) for row in months}
    return [by_month.get(m, ZERO) for m in range(1, 13)]


def month_by_month(this_year):
    """Rows for comparing this year with last year: (month name, this year or None if it hasn't
    started yet, last year)."""
    today = timezone.localdate()
    now, before = monthly_spent(this_year), monthly_spent(this_year - 1)
    return [(MONTH_NAMES[m - 1], now[m - 1] if (this_year, m) <= (today.year, today.month) else None,
             before[m - 1]) for m in range(1, 13)]


def year_summary(year):
    return summarise(Transaction.objects.with_current().filter(cur_date__year=year), year=year)


def all_time_summary():
    """Everything so far. Per member, months count from their own first transaction
    to the latest transaction in the app."""
    return summarise(Transaction.objects.with_current(), count_months=True)
