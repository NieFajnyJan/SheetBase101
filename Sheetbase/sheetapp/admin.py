from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import PlannedTransaction, Transaction, TransactionCorrection, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_filter = BaseUserAdmin.list_filter + ("is_shop",)
    # Email is required and unique on our model, so ask for it when adding a user.
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "email",
                    "first_name",
                    "last_name",
                    "usable_password",
                    "password1",
                    "password2",
                ),
            },
        ),
    )


class CorrectionInline(admin.TabularInline):
    """A transaction's corrections, newest first – shown, never changed here."""

    model = TransactionCorrection
    fk_name = "original"
    extra = 0
    fields = ("created_at", "created_by", "cancelled", "amount", "payer", "recipient", "description", "date", "reason")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False  # added from the Transaction corrections page instead

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    inlines = [CorrectionInline]
    list_display = ("date", "description", "amount", "payer", "recipient", "created_by", "created_at")
    list_filter = ("payer", "recipient", "date")
    search_fields = ("description",)  # also powers the "original" search box on corrections
    date_hierarchy = "date"
    readonly_fields = ("created_by", "created_at")

    def get_inlines(self, request, obj):
        return self.inlines if obj else []  # a new transaction has no corrections yet

    # Done transactions can be added and viewed, never edited or deleted.
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Whoever adds the entry in the admin is recorded as its author.
        obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(TransactionCorrection)
class TransactionCorrectionAdmin(admin.ModelAdmin):
    """Staff can add corrections here for any transaction (on the site only its author can).
    Like transactions, corrections are final: never edited or deleted."""

    list_display = ("created_at", "original", "cancelled", "amount", "payer", "recipient", "date", "created_by", "reason")
    list_filter = ("cancelled",)
    search_fields = ("description", "reason", "original__description")
    autocomplete_fields = ("original",)  # search box: type part of the description
    readonly_fields = ("created_by", "created_at")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        obj.created_by = request.user
        super().save_model(request, obj, form, change)


class StatusFilter(admin.SimpleListFilter):
    title = "status"
    parameter_name = "status"

    def lookups(self, request, model_admin):
        return [("open", "Open"), ("settled", "Settled")]

    def queryset(self, request, queryset):
        if self.value() == "open":
            return queryset.open()
        if self.value() == "settled":
            return queryset.settled()
        return queryset


@admin.register(PlannedTransaction)
class PlannedTransactionAdmin(admin.ModelAdmin):
    """Open debts. Open ones can be fixed or deleted here; settled ones are history."""

    list_display = ("description", "amount", "debtor", "creditor", "status", "created_by", "created_at")
    list_filter = (StatusFilter, "debtor", "creditor", "created_at")
    search_fields = ("description",)
    date_hierarchy = "created_at"
    readonly_fields = ("created_by", "created_at", "settled_by", "settled_at", "created_by_payment")

    @admin.display(description="status", ordering="settled_at")
    def status(self, obj):
        return "Settled" if obj.is_settled else "Open"

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and not (obj and obj.is_settled)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.is_settled)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
