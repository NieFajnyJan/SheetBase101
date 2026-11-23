import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import (DATE_FORMATS, AddPlannedForm, AddTransactionForm, CancelForm, CorrectionForm, EditPlannedForm,
                    HistoryFilterForm, ShopForm)
from .models import AlreadyAdded, PlannedTransaction, Transaction, TransactionCorrection, needs_matching, save_once
from .payments import PlanChanged, plan_payment, record_payment
from .splits import save_split
from .summary import all_time_summary, year_summary


def corrections():
    return Prefetch("corrections", queryset=TransactionCorrection.objects.select_related(
        "payer", "recipient", "created_by"))


def with_source(debts):
    """Debts with the purchase each came from (if any), as it is now, prefetched."""
    return debts.select_related("source").prefetch_related(
        Prefetch("source__corrections", queryset=TransactionCorrection.objects.all()))


def transactions_with_history():
    """Done transactions with their current values annotated, every version prefetched, and the debts
    each payment paid."""
    return Transaction.objects.with_current().select_related("payer", "recipient", "created_by").prefetch_related(
        corrections(),
        Prefetch("settled_debts", queryset=with_source(PlannedTransaction.objects.order_by("created_at", "pk"))))


def home(request):
    return render(request, "sheetapp/home.html")


@require_GET
def login_page(request):
    """Only shows the "Continue with ..." buttons – there are no local passwords."""
    if request.user.is_authenticated:
        return redirect("home")
    return render(request, "registration/login.html")


@login_required
def transactions(request):
    """One page: add a done transaction, add a missing shop, and see all done transactions
    with their total, optionally filtered."""
    paying = debt_to_mark_paid(request)
    if paying and paying.is_settled:  # e.g. Confirm clicked twice: the first click already paid it
        messages.info(request, f"Already paid: {paying}")
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
            messages.success(request, f"Added shop: {shop_form.save()}")
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
                    messages.info(request, f"Already added: {e.record}")
                else:
                    messages.success(request, f"Added: {obj}")
                return redirect(request.get_full_path())
            return confirm_payment(request, form, obj, paying)

    filter_form = HistoryFilterForm(request.GET)
    done = transactions_with_history().order_by("-cur_date", "-created_at")
    if filter_form.is_valid():
        done = filter_form.filter(done)
    return render(request, "sheetapp/transactions.html", {
        "form": form,
        "paying": paying,
        "shop_form": shop_form,
        "filter_form": filter_form,
        "filtered": any(request.GET.get(f) for f in filter_form.fields),
        "transactions": done,
        "total": done.aggregate(t=Sum("cur_counted"))["t"] or 0,  # corrected amounts; cancelled count 0
    })


def debt_to_mark_paid(request):
    """Mark paid opens this page with ?pay=<debt>: the debt, if the user is one of its two sides."""
    pk = request.GET.get("pay", "")
    if not pk:
        return None
    if not pk.isdigit():
        raise Http404
    debt = get_object_or_404(PlannedTransaction.objects.select_related("debtor", "creditor"), pk=pk)
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
    if "choosing" in request.POST:
        chosen = {int(pk) for pk in request.POST.getlist("debts") if pk.isdigit()}
    plan = None
    problem = paying and "confirm" in request.POST and mark_paid_problem(paying, payment, chosen)
    if problem:
        messages.error(request, problem)
    elif "confirm" in request.POST:
        try:
            # Checked before the plan: once the first click has paid the debts, the plan differs too.
            save_once(Transaction, payment.submission, lambda: record_payment(
                payment, chosen=chosen, expected_signature=request.POST.get("plan", "")))
        except AlreadyAdded as e:  # e.g. Confirm clicked twice
            messages.info(request, f"Already added: {e.record}")
            return redirect("planned" if paying else request.get_full_path())
        except PlanChanged as changed:
            plan = changed.plan
            messages.warning(request, "What this payment covers has changed – please check it again.")
        else:
            messages.success(request, f"Added: {payment}")
            return redirect("planned" if paying else request.get_full_path())
    plan = plan or plan_payment(payment.payer, payment.recipient, payment.amount, payment.description, chosen=chosen)
    return render(request, "sheetapp/confirm_payment.html",
                  {"form": form, "payment": payment, "plan": plan, "paying": paying})


def confirm_split(request, form, purchase=None):
    """Splitting: first shows each person's share; saves the purchase (if any) and the shares only
    when Confirm is clicked."""
    plan = form.split_plan()
    if "confirm" in request.POST:
        try:
            shares = save_split(plan, request.user, purchase)
        except AlreadyAdded:  # e.g. Confirm clicked twice
            messages.info(request, "This split was already added.")
        else:
            added = f"{purchase} and " if purchase else ""
            messages.success(request, f"Added: {added}{len(shares)} planned transaction{'s' if len(shares) != 1 else ''}"
                                      f" owed to {plan.creditor}.")
        return redirect(request.get_full_path())
    return render(request, "sheetapp/confirm_split.html", {"form": form, "plan": plan, "purchase": purchase})


def own_transaction(request, pk):
    """The transaction, if the logged-in user may correct it; otherwise 403."""
    transaction = get_object_or_404(transactions_with_history(), pk=pk)
    if not transaction.can_be_corrected_by(request.user):
        raise PermissionDenied("Only whoever added a transaction can correct it, and not once it's cancelled.")
    return transaction


@login_required
def correct_transaction(request, pk):
    transaction = own_transaction(request, pk)
    form = CorrectionForm(request.POST or None, original=transaction)
    if request.method == "POST" and form.is_valid():
        correction = form.save(commit=False)
        correction.created_by = request.user
        try:
            correction.save()
        except ValidationError as e:  # e.g. the loan got paid back after the form was checked
            form.add_error(None, e)
        else:
            messages.success(request, str(correction))
            return redirect("transactions")
    return render(request, "sheetapp/correct.html", {"form": form, "transaction": transaction, "cancelling": False})


@login_required
def cancel_transaction(request, pk):
    transaction = own_transaction(request, pk)
    form = CancelForm(request.POST or None, original=transaction)
    if request.method == "POST" and form.is_valid():
        correction = form.save(commit=False)
        correction.created_by = request.user
        try:
            correction.save()
        except ValidationError as e:  # the leftover got paid after the form was checked
            form.add_error(None, e)
        else:
            messages.success(request, str(correction))
            return redirect("transactions")
    return render(request, "sheetapp/correct.html", {"form": form, "transaction": transaction, "cancelling": True})


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
            messages.info(request, f"Already added: {e.record}")
        else:
            messages.success(request, f"Added: {debt}")
        return redirect("planned")
    debts = with_source(PlannedTransaction.objects.open().select_related("debtor", "creditor", "created_by"))
    return render(request, "sheetapp/planned.html", {
        "form": form,
        "debts": debts,
        "total": debts.aggregate(t=Sum("amount"))["t"] or 0,
    })


def open_debt(pk):
    """The planned transaction, while it's open: any member may edit or delete it then. Once paid, 403."""
    debt = get_object_or_404(PlannedTransaction, pk=pk)
    if debt.is_settled:
        raise PermissionDenied("This debt has been paid, so it can't be changed any more.")
    return debt


@login_required
def edit_planned(request, pk):
    debt = open_debt(pk)
    form = EditPlannedForm(request.POST or None, instance=debt)
    if request.method == "POST" and form.is_valid():
        messages.success(request, f"Saved: {form.save()}")
        return redirect("planned")
    return render(request, "sheetapp/planned_edit.html", {"form": form})


@login_required
def delete_planned(request, pk):
    """Deleting is only for mistakes; it takes the whole group with it. Paying goes through a payment."""
    debt = open_debt(pk)
    this = PlannedTransaction.objects.filter(pk=debt.pk)
    rows = list(this.with_groups().select_related("debtor", "creditor"))
    # Refused when someone in the group already paid.
    problems = [f"{r.debtor} already paid “{r.description}”." for r in rows if r.is_settled]
    if request.method == "POST" and not problems:
        try:
            deleted = this.delete_with_groups()[0]
        except ValidationError as e:  # someone paid in the meantime
            problems = e.messages
        else:
            messages.success(request, f"Deleted {deleted} planned transaction{'s' if deleted != 1 else ''}.")
            return redirect("planned")
    return render(request, "sheetapp/planned_delete.html", {"rows": rows, "problems": problems})


@login_required
def summary(request):
    """All time, then this year and last year. On 1 January "last year" is the year that just ended."""
    today = timezone.localdate()
    return render(request, "sheetapp/summary.html", {
        "all_time": all_time_summary(),
        "summaries": [year_summary(today.year), year_summary(today.year - 1)],
    })
