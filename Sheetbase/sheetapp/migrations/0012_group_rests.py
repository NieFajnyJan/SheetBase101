from django.db import migrations


def group_rests(apps, schema_editor):
    """Rests of partly paid debts added before they kept their debt's group join it now, like the ones
    added from now on: so a rest can't be deleted on its own once someone in the group has paid.
    Each change is written to the audit log by hand: saves made here don't reach its hooks."""
    from audit.log import record, snapshot

    Planned = apps.get_model("sheetapp", "PlannedTransaction")
    db = schema_editor.connection.alias
    # Oldest first, so the rest of a rest finds its group already set.
    rests = (Planned.objects.using(db).filter(group__isnull=True, created_by_payment__isnull=False)
             .select_related("created_by_payment", "debtor", "creditor").order_by("created_at", "pk"))

    def name(user):  # as User.__str__, which these models don't have
        return f"{user.first_name} {user.last_name}".strip() or user.username

    for rest in rests:
        payment = rest.created_by_payment
        if rest.debtor_id != payment.payer_id:  # owed the other way: an overpayment or a loan, not a rest
            continue
        # The debt paid only partly is the last one the payment settled (they're settled oldest first).
        partly = Planned.objects.using(db).filter(settled_by=payment).order_by("created_at", "pk").last()
        if partly is None or partly.group is None:
            continue
        before = snapshot(rest)
        rest.group = partly.group
        rest.save(update_fields=["group"])
        record("update", rest, before=before, after=snapshot(rest),  # named as PlannedTransaction.__str__
               object_repr=f"{name(rest.debtor)} owes {name(rest.creditor)} {rest.amount} zł – {rest.description}")


class Migration(migrations.Migration):

    dependencies = [
        ('sheetapp', '0011_submission'),
    ]

    operations = [
        migrations.RunPython(group_rests, migrations.RunPython.noop),
    ]
