from django.db import migrations, models

SHOP_USERNAME = "shop"


def mark_existing_shop(apps, schema_editor):
    apps.get_model("sheetapp", "User").objects.filter(username=SHOP_USERNAME).update(is_shop=True)


class Migration(migrations.Migration):

    dependencies = [
        ("sheetapp", "0004_transaction_date_range"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="is_shop",
            field=models.BooleanField(default=False, help_text="A shop, not a person. Can't log in.", verbose_name="shop"),
        ),
        migrations.RunPython(mark_existing_shop, migrations.RunPython.noop),
    ]
