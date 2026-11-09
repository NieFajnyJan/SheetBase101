"""Summaries (#13): per member, what they spent and paid – for one year, or all time.
Done transactions only, with their corrected values (#22); cancelled ones don't count."""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Max, Min, Sum

from .models import Transaction, User

ZERO = Decimal("0.00")


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
        Saldo netto here is total_spent – once planned transactions exist (#11), use the real balance.
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


def summarise(transactions, year=None, count_months=False):
    users = {u.pk: u for u in User.objects.all()}
    members = {pk: MemberRow(u) for pk, u in users.items() if not u.is_shop}
    shops = {pk: ShopRow(u) for pk, u in users.items() if u.is_shop}
    summary = Summary(year)
    latest = None  # date of the latest transaction in the period

    # One database query: amounts summed per (payer, recipient) pair, with the pair's first date.
    pairs = (transactions.filter(cur_cancelled=False).order_by().values("cur_payer", "cur_recipient")
             .annotate(amount=Sum("cur_amount"), first=Min("cur_date"), last=Max("cur_date")))
    for pair in pairs:
        # SQLite sums decimals with a long tail (192.1000000000000) – round to whole grosze.
        amount = pair["amount"].quantize(ZERO)
        payer, recipient = pair["cur_payer"], pair["cur_recipient"]
        summary.total += amount
        latest = max(latest or pair["last"], pair["last"])
        if payer in members and recipient in shops:
            members[payer].purchases += amount
            shops[recipient].sold += amount
        elif payer in shops and recipient in members:
            members[recipient].returns += amount
            shops[payer].returned += amount
        elif payer in members and recipient in members:
            members[payer].paid += amount
            members[recipient].received += amount
        for pk in (payer, recipient):
            if pk in members and (members[pk].first_date is None or pair["first"] < members[pk].first_date):
                members[pk].first_date = pair["first"]

    if count_months:
        for row in members.values():
            if row.first_date:
                row.months = complete_months(row.first_date, latest)

    summary.rows = list(members.values())  # User ordering: by name
    summary.shop_rows = [r for r in shops.values() if r.sold or r.returned]
    for column in ("purchases", "returns", "paid", "received"):
        setattr(summary, column, sum((getattr(r, column) for r in summary.rows), ZERO))
    return summary


def year_summary(year):
    return summarise(Transaction.objects.with_current().filter(cur_date__year=year), year=year)


def all_time_summary():
    """Everything so far. Per member, months count from their own first transaction
    to the latest transaction in the app."""
    return summarise(Transaction.objects.with_current(), count_months=True)
