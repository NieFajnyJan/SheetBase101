from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_date

from audit.models import Entry


class Command(BaseCommand):
    help = "Shows the audit log, newest last: who did what, when, from where, and what changed."

    def add_arguments(self, parser):
        parser.add_argument("--user", help="only entries by this username")
        parser.add_argument("--model", help='only this kind of record, e.g. "planned transaction"')
        parser.add_argument("--object", help="only this record's id (use with --model)")
        parser.add_argument("--action", choices=[a for a, _ in Entry.ACTIONS])
        parser.add_argument("--since", help="only from this date on (YYYY-MM-DD)")
        parser.add_argument("--limit", type=int, default=50, help="the latest N entries (default 50, 0 = all)")

    def handle(self, *args, user=None, model=None, object=None, action=None, since=None, limit=50, **options):
        entries = Entry.objects.all()
        if user:
            entries = entries.filter(username=user)
        if model:
            named = [label.lower() for label, shown in settings.AUDIT_MODELS.items() if shown == model.lower()]
            entries = entries.filter(model__in=named) if named else entries.filter(model__endswith=model.lower())
        if object:
            entries = entries.filter(object_id=object)
        if action:
            entries = entries.filter(action=action)
        if since:
            day = parse_date(since)
            if day is None:
                self.stderr.write(f"Not a date: {since} (use YYYY-MM-DD)")
                return
            entries = entries.filter(at__date__gte=day)
        if limit:
            entries = reversed(list(entries.order_by("-pk")[:limit]))
        for entry in entries:
            self.show(entry)

    def show(self, entry):
        for line in entry.lines():
            self.stdout.write(line)
