from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Sum
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import CancelForm, CorrectionForm, HistoryFilterForm, PlannedTransactionForm, ShopForm, TransactionForm
from .models import PlannedTransaction, Transaction, TransactionCorrection, needs_matching
from .payments import PlanChanged, plan_payment, record_payment
from .summary import all_time_summary, year_summary


def transactions_with_history():
    """Done transactions with their current values annotated and every version prefetched."""
    return Transaction.objects.with_current().select_related("payer", "recipient", "created_by").prefetch_related(
        Prefetch("corrections", queryset=TransactionCorrection.objects.select_related(
            "payer", "recipient", "created_by")))


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
    if paying:  # Mark paid: the form filled in from the debt
        form = TransactionForm(initial={"payer": paying.debtor, "recipient": paying.creditor,
                                        "amount": paying.amount, "description": paying.description})
    else:
        form = TransactionForm(initial={"payer": request.user})
    shop_form = ShopForm()

    if request.method == "POST" and "add_shop" in request.POST:
        shop_form = ShopForm(request.POST)
        if shop_form.is_valid():
            messages.success(request, f"Added shop: {shop_form.save()}")
            return redirect(request.get_full_path())  # keeps the current filters
    elif request.method == "POST":
        form = TransactionForm(request.POST)
        if form.is_valid() and "back" not in request.POST:  # Back: the form again, still filled in
            obj = form.save(commit=False)
            obj.created_by = request.user
            if not needs_matching(obj.payer, obj.recipient):  # a purchase or return: no debts involved
                obj.save()
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


def confirm_payment(request, form, payment, paying=None):
    """A payment between members: first shows which debts it settles and what's left over, with a
    tick box per debt to choose what it's for; saves only when Confirm is clicked."""
    # On the screen itself the ticks decide (none ticked is a valid choice); arriving from the form,
    # it's the debt being marked paid, or the debts are picked automatically.
    chosen = {paying.pk} if paying else None
    if "choosing" in request.POST:
        chosen = {int(pk) for pk in request.POST.getlist("debts") if pk.isdigit()}
    plan = None
    if "confirm" in request.POST:
        try:
            record_payment(payment, chosen=chosen, expected_signature=request.POST.get("plan", ""))
        except PlanChanged as changed:
            plan = changed.plan
            messages.warning(request, "What this payment covers has changed – please check it again.")
        else:
            messages.success(request, f"Added: {payment}")
            return redirect("planned" if paying else request.get_full_path())
    plan = plan or plan_payment(payment.payer, payment.recipient, payment.amount, payment.description, chosen=chosen)
    return render(request, "sheetapp/confirm_payment.html", {"form": form, "payment": payment, "plan": plan})


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
        correction.save()
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
    """Add a planned transaction (a debt) and see all open ones with their total."""
    form = PlannedTransactionForm(request.POST or None, initial={"creditor": request.user})
    if request.method == "POST" and form.is_valid():
        debt = form.save(commit=False)
        debt.created_by = request.user
        debt.save()
        messages.success(request, f"Added: {debt}")
        return redirect("planned")
    debts = PlannedTransaction.objects.open().select_related("debtor", "creditor", "created_by")
    return render(request, "sheetapp/planned.html", {
        "form": form,
        "debts": debts,
        "total": debts.aggregate(t=Sum("amount"))["t"] or 0,
    })


def own_debt(request, pk):
    """The planned transaction, if the logged-in user may edit or delete it; otherwise 403."""
    debt = get_object_or_404(PlannedTransaction, pk=pk)
    if not debt.can_be_changed_by(request.user):
        raise PermissionDenied("Only the person who is owed the money can change a debt, and only while it's open.")
    return debt


@login_required
def edit_planned(request, pk):
    debt = own_debt(request, pk)
    form = PlannedTransactionForm(request.POST or None, instance=debt)
    if request.method == "POST" and form.is_valid():
        messages.success(request, f"Saved: {form.save()}")
        return redirect("planned")
    return render(request, "sheetapp/planned_edit.html", {"form": form})


@login_required
def delete_planned(request, pk):
    """Deleting is only for mistakes; it takes the whole group with it. Paying goes through a payment."""
    debt = own_debt(request, pk)
    this = PlannedTransaction.objects.filter(pk=debt.pk)
    rows = list(this.with_groups().select_related("debtor", "creditor"))
    # Refused when someone in the group already paid, or a row in it is owed to someone else.
    problems = [f"{r.debtor} already paid “{r.description}”." for r in rows if r.is_settled]
    problems += [f"“{r}” is owed to {r.creditor}, not you." for r in rows if r.creditor_id != request.user.pk]
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
