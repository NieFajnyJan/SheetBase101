from django.db import migrations

# The database itself refuses to change or delete an entry, whoever asks – the app, the admin or a script.
FORWARD = [
    """CREATE TRIGGER audit_entry_no_update BEFORE UPDATE ON audit_entry
       BEGIN SELECT RAISE(ABORT, 'audit entries can''t be changed'); END""",
    """CREATE TRIGGER audit_entry_no_delete BEFORE DELETE ON audit_entry
       BEGIN SELECT RAISE(ABORT, 'audit entries can''t be deleted'); END""",
]
BACKWARD = ["DROP TRIGGER audit_entry_no_update", "DROP TRIGGER audit_entry_no_delete"]


class Migration(migrations.Migration):
    dependencies = [("audit", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
