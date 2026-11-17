from django.db import models
from django.utils import timezone


class Entry(models.Model):
    """One thing that happened: who, when, from where, and what changed (the record before and after).

    Kept in its own database and never changed or deleted: the database itself refuses that.
    Each entry stores the hash of the one before it, so a removed or edited entry breaks the chain."""

    CREATE, UPDATE, DELETE = "create", "update", "delete"
    LOGIN, LOGOUT, LOGIN_FAILED = "login", "logout", "login_failed"
    ACTIONS = [(CREATE, "added"), (UPDATE, "changed"), (DELETE, "deleted"),
               (LOGIN, "logged in"), (LOGOUT, "logged out"), (LOGIN_FAILED, "failed to log in")]

    at = models.DateTimeField(default=timezone.now)
    # Not a foreign key: users live in the other database, and a deleted user's entries must stay.
    user_id = models.IntegerField(null=True, blank=True)
    username = models.CharField(max_length=150, blank=True, help_text="Who, as they were called at the time.")
    ip = models.GenericIPAddressField(null=True, blank=True)
    action = models.CharField(max_length=20, choices=ACTIONS)
    model = models.CharField(max_length=100, blank=True, help_text="e.g. sheetapp.plannedtransaction")
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=300, blank=True)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["pk"]
        verbose_name_plural = "entries"

    def __str__(self):
        return self.lines()[0]

    def lines(self):
        """The entry as text anyone can read: a headline, then what changed."""
        from .display import lines

        return lines(self)

    def changes(self):
        """{field: (before, after)} for the fields that differ."""
        before, after = self.before or {}, self.after or {}
        return {f: (before.get(f), after.get(f)) for f in sorted(before.keys() | after.keys())
                if before.get(f) != after.get(f)}
