from django.core.management.base import BaseCommand

from audit.check import unlogged
from audit.log import record, snapshot
from audit.models import Entry


class Command(BaseCommand):
    help = ("Records, as they are now, the records the audit log has never seen. Run it once, right after "
            "setting the log up, so audit_check has something to compare the older records with.")

    def handle(self, *args, **options):
        missing = list(unlogged())
        for obj in missing:
            record(Entry.SNAPSHOT, obj, after=snapshot(obj))
        self.stdout.write(f"Recorded {len(missing)} record{'' if len(missing) == 1 else 's'}.")
