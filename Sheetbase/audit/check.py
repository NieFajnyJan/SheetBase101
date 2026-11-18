"""Whether the audit log can be trusted: nothing in it was changed or removed, and it agrees with the data.

Replaying a record's entries gives what the log says it holds now; that's compared with what the main
database really holds. A difference means a change the log didn't see (made outside the app, or a
failed write), or a log that was tampered with."""

from django.apps import apps
from django.conf import settings
from django.db import connections

from .display import fields_of, label, model_name, value
from .log import entry_hash, hashed_fields, snapshot
from .models import Entry
from .router import DB

TRIGGERS = {"audit_entry_no_update", "audit_entry_no_delete"}
DELETED = None


def chain_problems():
    """Entries changed or removed after they were written, and whether the database still refuses that."""
    problems = []
    with connections[DB].cursor() as cursor:
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'audit_entry'")
        missing = TRIGGERS - {name for name, in cursor.fetchall()}
    if missing:
        problems.append(f"the database no longer refuses changes to entries ({', '.join(sorted(missing))} removed)")
    prev = ""
    for entry in Entry.objects.order_by("pk").iterator():
        if entry.prev_hash != prev:
            problems.append(f"#{entry.pk}: the entry before it was changed or removed")
        if entry.hash != entry_hash(hashed_fields(entry)):
            problems.append(f"#{entry.pk}: changed after it was written")
        prev = entry.hash
    return problems


def logged_state():
    """What the log says each record holds now: {model: {id: values, or DELETED}}. A record the log
    has never seen isn't there."""
    state = {model.lower(): {} for model in settings.AUDIT_MODELS}
    changes = Entry.objects.filter(model__in=state, action__in=[Entry.CREATE, Entry.UPDATE, Entry.DELETE,
                                                                Entry.SNAPSHOT])
    for entry in changes.order_by("pk").iterator():
        state[entry.model][entry.object_id] = DELETED if entry.action == Entry.DELETE else entry.after
    return state


def records(model):
    return apps.get_model(model)._base_manager.using("default").order_by("pk")


def data_problems():
    """Records that differ from what the log says they hold."""
    problems = []
    for model, logged in logged_state().items():
        name, fields = model_name(model), fields_of(model)
        there = set()
        for obj in records(model).iterator():
            key = str(obj.pk)
            there.add(key)
            if key not in logged:
                problems.append(f"{name} {key}: not in the log")
                continue
            if logged[key] is DELETED:
                problems.append(f"{name} {key}: still there, but the log says it was deleted")
                continue
            now = snapshot(obj)
            # Only fields both have: one added to the app later isn't in older entries.
            for field in sorted(now.keys() & logged[key].keys()):
                was, real = logged[key][field], now[field]
                if was == real:
                    continue
                if field == "password":
                    problems.append(f"{name} {key}: password changed without the log knowing")
                    continue
                f = fields.get(field)
                problems.append(f"{name} {key}: {label(f, field)} is {value(f, real)}, the log says {value(f, was)}")
        for key, values in logged.items():
            if values is not DELETED and key not in there:
                problems.append(f"{name} {key}: deleted without the log knowing")
    return problems


def unlogged():
    """Records the log has never seen, e.g. those from before it was set up."""
    for model, logged in logged_state().items():
        yield from (obj for obj in records(model) if str(obj.pk) not in logged)
