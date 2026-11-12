from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Prefetch, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import CancelForm, CorrectionForm, HistoryFilterForm, ShopForm, TransactionForm
from .models import Transaction, TransactionCorrection, needs_matching
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
            return confirm_payment(request, form, obj)

    filter_form = HistoryFilterForm(request.GET)
    done = transactions_with_history().order_by("-cur_date", "-created_at")
    if filter_form.is_valid():
        done = filter_form.filter(done)
    return render(request, "sheetapp/transactions.html", {
        "form": form,
        "shop_form": shop_form,
        "filter_form": filter_form,
        "filtered": any(request.GET.get(f) for f in filter_form.fields),
        "transactions": done,
        "total": done.aggregate(t=Sum("cur_counted"))["t"] or 0,  # corrected amounts; cancelled count 0
    })


def confirm_payment(request, form, payment):
    """A payment between members: first shows which debts it settles and what's left over;
    saves only when Confirm is clicked."""
    plan = None
    if "confirm" in request.POST:
        try:
            record_payment(payment, expected_signature=request.POST.get("plan", ""))
        except PlanChanged as changed:
            plan = changed.plan
            messages.warning(request, "The debts changed in the meantime – please check again.")
        else:
            messages.success(request, f"Added: {payment}")
            return redirect(request.get_full_path())
    plan = plan or plan_payment(payment.payer, payment.recipient, payment.amount, payment.description)
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
def summary(request):
    """All time, then this year and last year. On 1 January "last year" is the year that just ended."""
    today = timezone.localdate()
    return render(request, "sheetapp/summary.html", {
        "all_time": all_time_summary(),
        "summaries": [year_summary(today.year), year_summary(today.year - 1)],
    })
