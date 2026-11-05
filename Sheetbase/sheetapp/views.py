from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from .forms import ShopForm, TransactionForm
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
def dev_sheet(request):
    """TEMPORARY page (staff only): add shops and done transactions without the admin and see
    them in a table. To be replaced by the real pages from #8 and #9."""
    if not request.user.is_staff:
        raise PermissionDenied

    form = TransactionForm(initial={"payer": request.user})
    shop_form = ShopForm()

    if request.method == "POST" and "add_shop" in request.POST:
        shop_form = ShopForm(request.POST)
        if shop_form.is_valid():
            messages.success(request, f"Added shop: {shop_form.save()}")
            return redirect("dev_sheet")
    elif request.method == "POST":
        form = TransactionForm(request.POST)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.created_by = request.user
            obj.save()
            messages.success(request, f"Added: {obj}")
            return redirect("dev_sheet")

    done = Transaction.objects.select_related("payer", "recipient", "created_by")
    return render(request, "sheetapp/dev_sheet.html", {
        "form": form,
        "shop_form": shop_form,
        "done": done,
        "done_total": done.aggregate(t=Sum("amount"))["t"] or 0,
    })
