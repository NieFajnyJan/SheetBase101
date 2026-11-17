import uuid

from django import forms
from django.utils import timezone
from django.utils.text import slugify

from .models import CORRECTABLE_FIELDS, MAX_YEARS_BACK, PlannedTransaction, Transaction, TransactionCorrection, User
from .splits import GROSZ, plan_split

# dd/mm/yy everywhere. A text box (the browser's own date picker can't change its format);
# the "datepicker" class turns it into a calendar on the page. 4-digit years are accepted too.
DATE_FORMATS = ["%d/%m/%y", "%d/%m/%Y"]
DATE_WIDGET = forms.DateInput(format=DATE_FORMATS[0],
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


SPLIT_FIELDS = ["split", "participants", "split_amount", "split_seed"]


class SplitMixin:
    """Adds splitting to an add form: a tick box, the members taking part (all ticked to start with)
    and a hidden seed that decides who gets leftover grosze. The form's own fields come first."""

    def add_split_fields(self, split_amount=False):
        self.fields["split"] = forms.BooleanField(required=False, label="Split between people")
        members = User.objects.filter(is_shop=False, is_active=True)
        self.fields["participants"] = forms.ModelMultipleChoiceField(
            members, required=False, widget=forms.CheckboxSelectMultiple, label="Who chips in",
            initial=lambda: list(members.values_list("pk", flat=True)))
        if split_amount:
            self.fields["split_amount"] = AmountField(
                label="Amount to split", required=False, max_digits=10, decimal_places=2,
                min_value=GROSZ, widget=forms.TextInput(attrs={"inputmode": "decimal"}),
                help_text="Leave empty to split the whole amount.")
            # Shown on the “?” next to the label.
            self.fields["split_amount"].hint = (
                "Only part of the purchase is shared? Say you bought 3 chairs for 4606,16 zł, but only one "
                "of them is for everyone: enter 1535,39 (one chair) here. The purchase is still recorded as "
                "4606,16 zł, and only 1535,39 zł is split between the people ticked.")
        self.fields["split_seed"] = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4, required=False)

    def main_fields(self):
        return [f for f in self.visible_fields() if f.name not in SPLIT_FIELDS]

    def drop_split_errors(self):
        """Without “Split between people” ticked its fields are ignored: what was typed there before
        unticking it (e.g. 0 to split) mustn't block the form from behind the hidden section."""
        for name in SPLIT_FIELDS:
            self._errors.pop(name, None)

    def check_split(self, data, creditor, amount, amount_field):
        """Checks a split of `amount` owed to `creditor` between the ticked people."""
        people = list(data.get("participants") or [])
        if creditor and not any(p != creditor for p in people):
            self.add_error("participants", f"Tick at least one person besides {creditor}.")
        elif amount and people and amount < GROSZ * len(people):
            self.add_error(amount_field, f"Too little to split between {len(people)} people.")

    def split_plan(self):
        data = self.cleaned_data
        return plan_split(self.split_creditor(), self.split_total(), data["participants"], data["description"],
                          data.get("split_seed") or uuid.uuid4(), tag=data.get("tag", ""))


class AddTransactionForm(SplitMixin, TransactionForm):
    """The add form on the Transactions page. Splitting a purchase: the shares become
    planned transactions owed to whoever paid."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_split_fields(split_amount=True)

    def split_creditor(self):
        return self.cleaned_data["payer"]

    def split_total(self):
        return self.cleaned_data.get("split_amount") or self.cleaned_data["amount"]

    def clean(self):
        data = super().clean()
        if not data.get("split"):
            self.drop_split_errors()
            return data
        payer, recipient, amount = data.get("payer"), data.get("recipient"), data.get("amount")
        if payer and recipient and (payer.is_shop or not recipient.is_shop):
            self.add_error("split", "Splitting is for purchases: “paid by” a member, “paid to” a shop.")
            return data
        part = data.get("split_amount")
        if part and amount and part > amount:
            self.add_error("split_amount", "Can't split more than the amount paid.")
        else:
            self.check_split(data, payer, part or amount, "split_amount" if part else "amount")
        return data


class AddPlannedForm(SplitMixin, PlannedTransactionForm):
    """The add form on the Planned page. When splitting, “owes” is replaced by the people who chip in:
    each gets a planned transaction for their share, owed to “owed to”."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["debtor"].required = False  # checked in clean(): only needed when not splitting
        self.add_split_fields()

    def split_creditor(self):
        return self.cleaned_data["creditor"]

    def split_total(self):
        return self.cleaned_data["amount"]

    def clean(self):
        data = super().clean()
        if data.get("split"):
            data["debtor"] = None  # replaced by the people who chip in
            self.check_split(data, data.get("creditor"), data.get("amount"), "amount")
        else:
            self.drop_split_errors()
            if not data.get("debtor") and "debtor" not in self.errors:
                self.add_error("debtor", forms.Field.default_error_messages["required"])
        return data


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
