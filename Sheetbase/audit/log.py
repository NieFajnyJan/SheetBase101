"""Writing the audit log.

Entries are written after the change is committed to the main database (so the log only records what
really happened), each with the hash of the one before it. Who and from where come from the request
being handled; outside a request (shell, management commands) the entry has no user or IP."""

import hashlib
import json
import logging
from contextvars import ContextVar

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from .router import DB

logger = logging.getLogger(__name__)
# Each entry, as it's written – printed in the server's terminal (see LOGGING in settings).
entries_logger = logging.getLogger("audit.entries")

# Never stored: password hashes; logins are recorded as their own entries instead of last_login.
SECRET_FIELDS = {"password"}
SKIPPED_FIELDS = {"last_login"}

_request = ContextVar("audit_request", default=None)


def current_request():
    return _request.get()


class RequestMiddleware:
    """Remembers the request being handled, so changes made during it know who made them and from where."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _request.set(request)
        try:
            return self.get_response(request)
        finally:
            _request.reset(token)


def client_ip(request):
    return request.META.get("REMOTE_ADDR") or None if request is not None else None


def actor(request=None, user=None):
    """(user_id, username, ip) of whoever is acting now."""
    request = request or current_request()
    if user is None and request is not None:
        user = getattr(request, "user", None)
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    return (user.pk if user else None, user.get_username() if user else "", client_ip(request))


def normalise(data):
    """Values as the log keeps them: JSON-safe, the same whether read from the database or not."""
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


def snapshot(obj):
    """A record's stored values, by column name. A secret is replaced by a fingerprint of it, so a change
    still shows without the secret itself being kept."""
    data = {}
    for field in obj._meta.concrete_fields:
        if field.attname in SKIPPED_FIELDS:
            continue
        value = field.value_from_object(obj)
        if field.attname in SECRET_FIELDS:
            value = hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else ""
        data[field.attname] = value
    return normalise(data)


def stored(obj):
    """A snapshot of what the database holds for this record now (None if it's not there)."""
    fresh = type(obj)._base_manager.using(obj._state.db or "default").filter(pk=obj.pk).first()
    return snapshot(fresh) if fresh is not None else None


def entry_hash(fields):
    payload = json.dumps(fields, sort_keys=True, cls=DjangoJSONEncoder, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def hashed_fields(entry):
    """What an entry's hash covers: everything in it, including the hash of the entry before."""
    return {
        "at": entry.at.isoformat(), "user_id": entry.user_id, "username": entry.username, "ip": entry.ip,
        "action": entry.action, "model": entry.model, "object_id": entry.object_id,
        "object_repr": entry.object_repr, "before": entry.before, "after": entry.after,
        "prev_hash": entry.prev_hash,
    }


def _write(fields):
    from .models import Entry

    # The audit database starts every transaction with a write lock (see settings), so two entries
    # written at once can't both chain onto the same previous one.
    with transaction.atomic(using=DB):
        prev = Entry.objects.using(DB).order_by("-pk").values_list("hash", flat=True).first() or ""
        entry = Entry(prev_hash=prev, **fields)
        entry.hash = entry_hash(hashed_fields(entry))
        entry.save(using=DB)
    entries_logger.info("\n".join(entry.lines()))
    return entry


def record(action, obj=None, before=None, after=None, request=None, user=None, username=None, model=None,
           object_repr=None):
    """Logs one change (or login) once the main database commits it. Never breaks what's being logged:
    a failure to write the entry is reported in the server log instead. `object_repr` names the record
    when str(obj) can't, e.g. for the models a migration works with."""
    user_id, name, ip = actor(request, user)
    if object_repr is None:
        object_repr = str(obj) if obj is not None else ""
    fields = {
        "at": timezone.now(), "user_id": user_id, "username": username if username is not None else name,
        "ip": ip, "action": action, "before": before, "after": after,
        "model": model or (obj._meta.label_lower if obj is not None else ""),
        "object_id": str(obj.pk) if obj is not None and obj.pk is not None else "",
        "object_repr": object_repr[:300],
    }

    def write():
        try:
            _write(fields)
        except Exception:
            logger.exception("Couldn't write audit entry: %s %s %s", action, fields["model"], fields["object_id"])

    transaction.on_commit(write, using="default")
