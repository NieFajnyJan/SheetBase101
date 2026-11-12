from django import forms
from django.utils import timezone
from django.utils.text import slugify

from .models import CORRECTABLE_FIELDS, MAX_YEARS_BACK, PlannedTransaction, Transaction, TransactionCorrection, User

# dd/mm/yy everywhere. A text box (the browser's own date picker can't change its format);
# the "datepicker" class turns it into a calendar on the page. 4-digit years are accepted too.
DATE_FORMATS = ["%d/%m/%y", "%d/%m/%Y"]
DATE_WIDGET = forms.DateInput(format="%d/%m/%y",
                              attrs={"class": "datepicker", "placeholder": "dd/mm/yy", "autocomplete": "off"})


class AmountField(forms.DecimalField):
    """Accepts both 12.49 and 12,49 – Polish users type commas."""

    def to_python(self, value):
        if isinstance(value, str):
            value = value.replace(",", ".")
        return super().to_python(value)


class TransactionForm(forms.ModelForm):
    class Meta:
        model = Transaction
        fields = ["amount", "payer", "recipient", "description", "date"]
        field_classes = {"amount": AmountField}
        # A text box, not type=number: number boxes reject a comma in some browsers.
        widgets = {"date": DATE_WIDGET, "amount": forms.TextInput(attrs={"inputmode": "decimal"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = DATE_FORMATS


class CorrectionForm(TransactionForm):
    """The add form, pre-filled with the transaction's current values, plus a reason."""

    class Meta(TransactionForm.Meta):
        model = TransactionCorrection
        fields = CORRECTABLE_FIELDS + ["reason"]

    def __init__(self, *args, original, **kwargs):
        self.current = original.current
        kwargs["initial"] = {f: getattr(self.current, f) for f in CORRECTABLE_FIELDS}
        super().__init__(*args, **kwargs)
        self.instance.original = original

    def clean(self):
        data = super().clean()
        if all(data.get(f) == getattr(self.current, f) for f in CORRECTABLE_FIELDS):
            raise forms.ValidationError("Nothing was changed.")
        return data


class CancelForm(forms.ModelForm):
    """Cancelling = a correction that keeps the current values, marked cancelled. Only asks why."""

    class Meta:
        model = TransactionCorrection
        fields = ["reason"]

    def __init__(self, *args, original, **kwargs):
        super().__init__(*args, **kwargs)
        for f in CORRECTABLE_FIELDS:
            setattr(self.instance, f, getattr(original.current, f))
        self.instance.original = original
        self.instance.cancelled = True


class PlannedTransactionForm(forms.ModelForm):
    """Add or edit a debt. Shops aren't offered: they never owe or are owed."""

    class Meta:
        model = PlannedTransaction
        fields = ["debtor", "creditor", "amount", "description", "tag"]
        field_classes = {"amount": AmountField}
        widgets = {"amount": forms.TextInput(attrs={"inputmode": "decimal"})}


MONTHS = [(m, timezone.datetime(2000, m, 1).strftime("%B")) for m in range(1, 13)]


class HistoryFilterForm(forms.Form):
    """Narrows the history. All fields optional; an empty form shows everything."""

    payer = forms.ModelChoiceField(User.objects.all(), required=False, label="paid by", empty_label="anyone")
    recipient = forms.ModelChoiceField(User.objects.all(), required=False, label="paid to", empty_label="anyone")
    year = forms.TypedChoiceField(coerce=int, required=False, empty_value=None)
    month = forms.TypedChoiceField(coerce=int, required=False, empty_value=None,
                                   choices=[("", "whole year")] + MONTHS)
    hide_cancelled = forms.BooleanField(required=False, label="hide cancelled")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Transactions can only be dated this year or MAX_YEARS_BACK years before.
        this_year = timezone.localdate().year
        self.fields["year"].choices = [("", "any")] + [
            (y, y) for y in range(this_year, this_year - MAX_YEARS_BACK - 1, -1)]

    def clean(self):
        data = super().clean()
        if data.get("month") and not data.get("year"):
            self.add_error("year", "Pick a year for the month.")
        return data

    def filter(self, transactions):
        """Expects Transaction.objects.with_current(): filters on current (corrected) values."""
        data = self.cleaned_data
        if data.get("payer"):
            transactions = transactions.filter(cur_payer=data["payer"].pk)
        if data.get("recipient"):
            transactions = transactions.filter(cur_recipient=data["recipient"].pk)
        if data.get("year"):
            transactions = transactions.filter(cur_date__year=data["year"])
            if data.get("month"):
                transactions = transactions.filter(cur_date__month=data["month"])
        if data.get("hide_cancelled"):
            transactions = transactions.filter(cur_cancelled=False)
        return transactions


class ShopForm(forms.Form):
    name = forms.CharField(label="shop name", max_length=100)

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        self.username = "shop-" + slugify(name, allow_unicode=True)
        if self.username == "shop-":
            raise forms.ValidationError("Enter a name with letters or digits.")
        if (User.objects.filter(username=self.username).exists()
                or User.objects.filter(is_shop=True, first_name__iexact=name).exists()):
            raise forms.ValidationError("This shop already exists.")
        return name

    def save(self):
        shop = User(username=self.username, email=f"{self.username}@sheetbase.invalid",
                    first_name=self.cleaned_data["name"], is_shop=True)
        shop.set_unusable_password()
        shop.save()
        return shop
