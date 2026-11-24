from django.db import migrations, models


def number_corrections(apps, schema_editor):
    """Corrections made so far get their version: 1, 2, ... per transaction, oldest first.
    Not written to the audit log: it's a counter, not something anyone changed."""
    Correction = apps.get_model("sheetapp", "TransactionCorrection")
    db = schema_editor.connection.alias
    numbers = {}
    for correction in Correction.objects.using(db).order_by("created_at", "pk"):
        numbers[correction.original_id] = numbers.get(correction.original_id, 0) + 1
        correction.version = numbers[correction.original_id]
        correction.save(update_fields=["version"])


class Migration(migrations.Migration):

    dependencies = [
        ("sheetapp", "0012_group_rests"),
    ]

    operations = [
        migrations.AddField(
            model_name="plannedtransaction",
            name="version",
            field=models.PositiveIntegerField(default=0, editable=False),
        ),
        migrations.AddField(
            model_name="transactioncorrection",
            name="version",
            field=models.PositiveIntegerField(editable=False, null=True),
        ),
        migrations.RunPython(number_corrections, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="transactioncorrection",
            name="version",
            field=models.PositiveIntegerField(editable=False),
        ),
        migrations.AddConstraint(
            model_name="transactioncorrection",
            constraint=models.UniqueConstraint(fields=("original", "version"), name="correction_version_unique"),
        ),
    ]
