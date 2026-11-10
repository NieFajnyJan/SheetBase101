from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Prefetch, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET

from .forms import CancelForm, CorrectionForm, HistoryFilterForm, ShopForm, TransactionForm
from .models import Transaction, TransactionCorrection
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
        if form.is_valid():
            obj = form.save(commit=False)
            obj.created_by = request.user
            obj.save()
            messages.success(request, f"Added: {obj}")
            return redirect(request.get_full_path())

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
        correction.save()
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
