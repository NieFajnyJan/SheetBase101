from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Transaction, User


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


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("date", "description", "amount", "payer", "recipient", "created_by", "created_at")
    list_filter = ("payer", "recipient", "date")
    search_fields = ("description",)
    date_hierarchy = "date"
    readonly_fields = ("created_by", "created_at")

    # Done transactions can be added and viewed, never edited or deleted.
    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # Whoever adds the entry in the admin is recorded as its author.
        obj.created_by = request.user
        super().save_model(request, obj, form, change)
