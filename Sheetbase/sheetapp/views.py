import uuid
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Sum
from django.db.models.functions import ExtractYear
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import (
    DATE_FORMATS,
    AddPlannedForm,
    AddTransactionForm,
    CancelForm,
    CorrectionForm,
    EditPlannedForm,
    HistoryFilterForm,
    ShopForm,
)
from .models import (
    AlreadyAdded,
    GroupChanged,
    PlannedTransaction,
    Transaction,
    TransactionCorrection,
    changed_rows,
    needs_matching,
    rows_signature,
    save_once,
)
from .payments import PlanChanged, cancel_effects, plan_payment, record_payment
from .splits import save_split
from .summary import all_time_summary, month_by_month, year_summary


class DebtGone(Exception):
    """The debt a page is about was deleted or paid since it was opened, e.g. in another tab."""


def back_to_planned_if_gone(view):
    """When the debt is gone (DebtGone), back to the Planned page, where the warning shows."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except DebtGone:
            return redirect("planned")
    return wrapper


def corrections():
    return Prefetch("corrections", queryset=TransactionCorrection.objects.select_related(
        "payer", "recipient", "created_by"))


def transactions_with_history():
    """Done transactions with their current values annotated, every version prefetched, and the debts
    each payment paid."""
    return Transaction.objects.with_current().select_related("payer", "recipient", "created_by").prefetch_related(
        corrections(),
        Prefetch("settled_debts", queryset=PlannedTransaction.objects.order_by("created_at", "pk")))


def home(request):
    return render(request, "sheetapp/home.html")


@require_GET
def login_page(request):
    """Only shows the "Continue with ..." buttons – there are no local passwords."""
    if request.user.is_authenticated:
        return redirect("home")
    return render(request, "registration/login.html")


@login_required
@back_to_planned_if_gone
def transactions(request):
    """One page: add a done transaction, add a missing shop, and see all done transactions
    with their total, optionally filtered."""
    paying = debt_to_mark_paid(request)
    if paying and paying.is_settled:  # e.g. Confirm clicked twice: the first click already paid it
        messages.info(request, f"Nothing was saved. Already paid: {paying}")
        return redirect("planned")
    if paying:  # Mark paid: the payment filled in from the debt
        details = {"payer": paying.debtor.pk, "recipient": paying.creditor.pk, "amount": str(paying.amount),
                   "description": paying.description, "date": timezone.localdate().strftime(DATE_FORMATS[0]),
                   "submission": str(uuid.uuid4())}
        if request.method == "GET":  # straight to the confirmation screen; its Back opens the form
            form = AddTransactionForm(details, paying=paying)
            if form.is_valid():
                payment = form.save(commit=False)
                payment.created_by = request.user
                return confirm_payment(request, form, payment, paying)
        form = AddTransactionForm(initial=details, paying=paying)
    else:
        form = AddTransactionForm(initial={"payer": request.user})
    shop_form = ShopForm()

    if request.method == "POST" and "add_shop" in request.POST:
        shop_form = ShopForm(request.POST)
        if shop_form.is_valid():
            messages.success(request, f"Saved. Added shop: {shop_form.save()}")
            return redirect(request.get_full_path())  # keeps the current filters
    elif request.method == "POST":
        form = AddTransactionForm(request.POST, paying=paying)
        if form.is_valid() and "back" not in request.POST:  # Back: the form again, still filled in
            obj = form.save(commit=False)
            obj.created_by = request.user
            if form.cleaned_data["split"]:
                return confirm_split(request, form, purchase=obj)
            if not needs_matching(obj.payer, obj.recipient):  # a purchase or return: no debts involved
                try:
                    save_once(Transaction, obj.submission, obj.save)
                except AlreadyAdded as e:  # e.g. Add clicked twice
                    messages.info(request, f"Not saved again. Already added: {e.record}")
                else:
                    messages.success(request, f"Saved. Added: {obj}")
                return redirect(request.get_full_path())
            return confirm_payment(request, form, obj, paying)

    years = Transaction.objects.with_current().annotate(y=ExtractYear("cur_date")).values_list("y", flat=True)
    # Its own ids: the add form above has a payer and a recipient too, and a label must point at one box.
    filter_form = HistoryFilterForm(request.GET, years=set(years), auto_id="filter_%s")
    done = transactions_with_history().order_by("-cur_date", "-created_at")
    # A filter that can't be read shows nothing, rather than everything under a total that looks filtered.
    valid = filter_form.is_valid()
    if valid:
        done = filter_form.filter(done)
    return render(request, "sheetapp/transactions.html", {
        "form": form,
        "paying": paying,
        "shop_form": shop_form,
        "filter_form": filter_form,
        "filters_valid": valid,
        "kept_ticks": kept_ticks(request),
        "filtered": any(request.GET.get(f) for f in filter_form.fields),
        "transactions": done if valid else [],
        # Corrected amounts; cancelled ones count 0.
        "total": (done.aggregate(t=Sum("cur_counted"))["t"] or 0) if valid else None,
    })


def debt_to_mark_paid(request):
    """Mark paid opens this page with ?pay=<debt>: the debt, if the user is one of its two sides."""
    pk = request.GET.get("pay", "")
    if not pk:
        return None
    if not pk.isdigit():
        raise Http404
    debt = PlannedTransaction.objects.select_related("debtor", "creditor").filter(pk=pk).first()
    if debt is None:
        open_debt(request, pk)  # deleted: warns and goes back to Planned
    if not debt.can_be_marked_paid_by(request.user):
        raise PermissionDenied("Only the person who owes or is owed the money can mark it as paid.")
    return debt


def mark_paid_problem(paying, payment, chosen):
    """Why a Mark paid payment can't be saved like this, if it can't: it must pay (at least part of)
    the debt Mark paid was clicked on. Other debts it may pay too; a payment for them alone, or a loan,
    is added on the Transactions page."""
    if paying.pk not in chosen:
        return (f"Tick the debt this payment is for: “{paying.description}”. To pay other debts or lend "
                "money instead, add the payment on the Transactions page.")
    plan = plan_payment(payment.payer, payment.recipient, payment.amount, payment.description, chosen=chosen)
    if paying.pk not in {d.pk for d in plan.covered}:
        return (f"The money runs out before “{paying.description}”, the debt this payment is for. "
                "Untick other debts or raise the amount.")
    return None


def confirm_payment(request, form, payment, paying=None):
    """A payment between members: first shows which debts it settles and what's left over, with a
    tick box per debt to choose what it's for; saves only when Confirm is clicked."""
    # On the screen itself the ticks decide (none ticked is a valid choice); arriving from the form,
    # it's the debt being marked paid, or the debts are picked automatically.
    chosen = {paying.pk} if paying else None
    ticked = {int(pk) for pk in request.POST.getlist("debts") if pk.isdigit()}
    if "choosing" in request.POST:
        chosen = ticked
    elif request.POST.get("ticked_for") == ticks_key(payment):
        # Back to the form and Add again: the ticks made before, as long as it's between the same people.
        chosen = ticked | (chosen or set())
    plan = None
    changed_debts, leftover_changed = set(), False  # what differs from the screen Confirm was clicked on
    problem = paying and "confirm" in request.POST and mark_paid_problem(paying, payment, chosen)
    if problem:
        messages.error(request, f"Not saved. {problem}")
    elif "confirm" in request.POST:
        try:
            # Once the first click has paid the debts, the plan differs too: then it's AlreadyAdded.
            save_once(Transaction, payment.submission, lambda: record_payment(
                payment, chosen=chosen, expected_signature=request.POST.get("plan", "")), also=(PlanChanged,))
        except AlreadyAdded as e:  # e.g. Confirm clicked twice
            messages.info(request, f"Not saved again. Already added: {e.record}")
            return redirect("planned" if paying else request.get_full_path())
        except PlanChanged as changed:
            plan = changed.plan
            changed_debts, leftover_changed = plan.changes_since(request.POST.get("plan", ""))
            messages.warning(request, "Nothing was saved. What this payment covers has changed since you "
                                      "saw it – please check it again.")
        else:
            messages.success(request, f"Saved. Added: {payment}")
            return redirect("planned" if paying else request.get_full_path())
    plan = plan or plan_payment(payment.payer, payment.recipient, payment.amount, payment.description, chosen=chosen)
    return render(request, "sheetapp/confirm_payment.html", {
        "form": form, "payment": payment, "plan": plan, "paying": paying, "ticks_key": ticks_key(payment),
        "changed_debts": changed_debts, "leftover_changed": leftover_changed,
    })


def ticks_key(payment):
    """Which two people ticked debts belong to: kept over Back only while the payment is between them."""
    return f"{payment.payer_id}-{payment.recipient_id}"


def kept_ticks(request):
    """Back from the payment confirmation: the debts ticked there, to send along with the form again."""
    if "back" not in request.POST or "choosing" not in request.POST:
        return None
    return {"debts": request.POST.getlist("debts"), "key": request.POST.get("ticked_for", "")}


def confirm_split(request, form, purchase=None):
    """Splitting: first shows each person's share; saves the purchase (if any) and the shares only
    when Confirm is clicked."""
    plan = form.split_plan()
    if "confirm" in request.POST:
        try:
            shares = save_split(plan, request.user, purchase)
        except AlreadyAdded:  # e.g. Confirm clicked twice
            messages.info(request, "Not saved again. This split was already added.")
        else:
            added = f"{purchase} and " if purchase else ""
            messages.success(request, f"Saved. Added: {added}{len(shares)} planned transaction{'s' if len(shares) != 1 else ''}"
                                      f" owed to {plan.creditor}.")
        return redirect(request.get_full_path())
    return render(request, "sheetapp/confirm_split.html", {"form": form, "plan": plan, "purchase": purchase})


def own_transaction(request, pk):
    """The transaction, if the logged-in user may correct it; otherwise 403."""
    candidates = Transaction.objects.select_related("payer", "recipient").prefetch_related(corrections())
    transaction = get_object_or_404(candidates, pk=pk)
    if not transaction.can_be_corrected_by(request.user):
        raise PermissionDenied("Only whoever added a transaction can correct it, and not once it's cancelled.")
    return transaction


@login_required
def correct_transaction(request, pk):
    return _change_transaction(request, pk, cancelling=False)


@login_required
def cancel_transaction(request, pk):
    return _change_transaction(request, pk, cancelling=True)


def _change_transaction(request, pk, *, cancelling):
    transaction = own_transaction(request, pk)
    form_class = CancelForm if cancelling else CorrectionForm
    form = form_class(request.POST or None, original=transaction)
    if request.method == "POST" and form.is_valid():
        correction = form.save(commit=False)
        correction.created_by = request.user
        try:
            correction.save()
        except ValidationError as e:  # a related debt may have been paid after form validation
            form.add_error(None, e)
        else:
            messages.success(request, f"Saved. {correction}")
            return redirect("transactions")
    return render(request, "sheetapp/correct.html", {
        "form": form, "transaction": transaction, "cancelling": cancelling,
        "effects": cancel_effects(transaction) if cancelling else None,
    })


@login_required
def planned(request):
    """Add a planned transaction (a debt), or split one between people, and see all open ones with their total."""
    form = AddPlannedForm(request.POST or None, initial={"creditor": request.user})
    if request.method == "POST" and form.is_valid() and "back" not in request.POST:
        if form.cleaned_data["split"]:
            return confirm_split(request, form)
        debt = form.save(commit=False)
        debt.created_by = request.user
        try:
            save_once(PlannedTransaction, debt.submission, debt.save)
        except AlreadyAdded as e:  # e.g. Add clicked twice
            messages.info(request, f"Not saved again. Already added: {e.record}")
        else:
            messages.success(request, f"Saved. Added: {debt}")
        return redirect("planned")
    debts = (PlannedTransaction.objects.open().select_related("debtor", "creditor", "source")
             .prefetch_related("source__corrections"))
    return render(request, "sheetapp/planned.html", {
        "form": form,
        "debts": debts,
        "total": debts.aggregate(t=Sum("amount"))["t"] or 0,
        "owed_to_me": balances(debts, request.user, "creditor"),
        "i_owe": balances(debts, request.user, "debtor"),
    })


def balances(debts, user, side):
    """The open debts where `user` is on `side` ("creditor": owed to them, "debtor": they owe), added up
    per other person: ([(person, amount), ...] by name, total)."""
    other = "debtor" if side == "creditor" else "creditor"
    per_person = {}
    for debt in debts:
        if getattr(debt, f"{side}_id") == user.pk:
            person = getattr(debt, other)
            per_person[person] = per_person.get(person, 0) + debt.amount
    rows = sorted(per_person.items(), key=lambda row: str(row[0]).lower())
    return rows, sum((amount for _, amount in rows), Decimal("0.00"))


def open_debt(request, pk, nothing_done="Nothing was saved."):
    """The planned transaction, while it's open: any member may edit, delete or pay it then. Deleted or
    paid already: warns and raises DebtGone. `nothing_done` starts the warning when something was sent."""
    debt = PlannedTransaction.objects.select_related("debtor", "creditor").filter(pk=pk).first()
    if debt is None or debt.is_settled:
        what = "was deleted" if debt is None else "has been paid"
        start = f"{nothing_done} " if request.method == "POST" else ""
        messages.warning(request, f"{start}This planned transaction {what} in the meantime (maybe in another tab), "
                                  "so it can't be changed any more.")
        raise DebtGone
    return debt


@login_required
@back_to_planned_if_gone
def edit_planned(request, pk):
    debt = open_debt(request, pk)
    form = EditPlannedForm(request.POST or None, instance=debt)
    if request.method == "POST" and form.is_valid():
        try:
            saved = form.save()
        except ValidationError as e:  # changed by someone else at the same moment
            form.add_error(None, e)
        else:
            messages.success(request, f"Saved. Changed: {saved}")
            return redirect("planned")
    return render(request, "sheetapp/planned_edit.html", {"form": form})


@login_required
@back_to_planned_if_gone
def delete_planned(request, pk):
    """Deleting is only for mistakes; it takes the whole group with it. Paying goes through a payment."""
    debt = open_debt(request, pk, nothing_done="Nothing was deleted.")
    this = PlannedTransaction.objects.filter(pk=debt.pk)
    rows = list(this.with_groups().select_related("debtor", "creditor"))
    # Refused when someone in the group already paid.
    problems = [f"{r.debtor} already paid “{r.description}”." for r in rows if r.is_settled]
    changed = set()
    if request.method == "POST" and not problems:
        shown = request.POST.get("rows")  # None from a page opened before this check: not compared
        try:
            deleted = this.delete_with_groups(expected=shown)[0]
        except GroupChanged:  # someone changed the group or a row in it after the page was opened
            changed = changed_rows(rows, shown)
            messages.warning(request, "Nothing was deleted. What would be deleted changed after you opened "
                                      "this page – check it and delete again.")
        except ValidationError as e:  # someone paid in the meantime
            problems = e.messages
        else:
            messages.success(request, f"Deleted {deleted} planned transaction{'s' if deleted != 1 else ''}.")
            return redirect("planned")
    return render(request, "sheetapp/planned_delete.html", {
        "rows": rows, "problems": problems, "signature": rows_signature(rows), "changed": changed})


@login_required
def summary(request):
    """All time, then this year and last year. On 1 January "last year" is the year that just ended."""
    today = timezone.localdate()
    return render(request, "sheetapp/summary.html", {
        "all_time": all_time_summary(),
        "summaries": [year_summary(today.year), year_summary(today.year - 1)],
        "months": month_by_month(today.year),
        "this_year": today.year,
    })
