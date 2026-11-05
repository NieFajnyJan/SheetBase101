from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion

SHOP_USERNAME = "shop"


def reset_transactions_and_create_shop(apps, schema_editor):
    """Old transactions (without "paid to") are not kept. The shop is an ordinary user
    that purchases are paid to; "!" as password means it can't log in."""
    apps.get_model("sheetapp", "Transaction").objects.all().delete()
    apps.get_model("sheetapp", "User").objects.get_or_create(
        username=SHOP_USERNAME,
        defaults={"email": "shop@sheetbase.invalid", "first_name": "Shop", "password": "!"},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sheetapp", "0002_transaction"),
    ]

    operations = [
        migrations.RunPython(reset_transactions_and_create_shop, migrations.RunPython.noop),
        migrations.AddField(
            model_name="transaction",
            name="recipient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="received_transactions",
                to=settings.AUTH_USER_MODEL, verbose_name="paid to",
            ),
        ),
        migrations.AddConstraint(
            model_name="transaction",
            constraint=models.CheckConstraint(
                condition=~models.Q(payer=models.F("recipient")),
                name="transaction_payer_is_not_recipient",
                violation_error_message="“Paid by” and “paid to” must be different people.",
            ),
        ),
    ]
