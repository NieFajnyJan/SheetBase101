from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from .forms import HistoryFilterForm, ShopForm, TransactionForm
from .models import Transaction


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
    """One page: add a done transaction (#8), add a missing shop, and see all done transactions
    with their total (#9), optionally filtered."""
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
    done = Transaction.objects.select_related("payer", "recipient", "created_by")
    if filter_form.is_valid():
        done = filter_form.filter(done)
    return render(request, "sheetapp/transactions.html", {
        "form": form,
        "shop_form": shop_form,
        "filter_form": filter_form,
        "filtered": any(request.GET.get(f) for f in filter_form.fields),
        "transactions": done,
        "total": done.aggregate(t=Sum("amount"))["t"] or 0,
    })
