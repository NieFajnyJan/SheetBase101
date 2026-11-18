from django.core.management.base import BaseCommand, CommandError

from audit.check import chain_problems, data_problems


class Command(BaseCommand):
    help = ("Checks the audit log can be trusted: no entry was changed or removed, and every record in the "
            "app holds what the log says it does.")

    def handle(self, *args, **options):
        problems = chain_problems() + data_problems()
        for problem in problems:
            self.stdout.write(problem)
        if problems:
            raise CommandError(f"{len(problems)} problem{'s' if len(problems) > 1 else ''} found")
        self.stdout.write("The log is intact and agrees with the data.")
