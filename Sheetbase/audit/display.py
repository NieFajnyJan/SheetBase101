"""Audit entries as text anyone can read: the app's own names for things, field labels instead of
column names, people and payments by name instead of by id, money and dates as the app shows them.
Only the display changes; the log itself keeps the raw values."""

from decimal import Decimal, InvalidOperation

from django.apps import apps
from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

DATE = "%d/%m/%y"
NOTHING = "—"
HIDDEN = {"id"}  # shown in the headline already


def model_class(label):
    try:
        return apps.get_model(label)
    except (LookupError, ValueError):
        return None


def model_name(label):
    """What the app calls this kind of record, e.g. "planned transaction"."""
    for name, shown in settings.AUDIT_MODELS.items():
        if name.lower() == label:
            return shown
    model = model_class(label)
    return str(model._meta.verbose_name) if model else label


def fields_of(label):
    model = model_class(label)
    return {f.attname: f for f in model._meta.concrete_fields} if model else {}


def value(field, raw):
    if raw is None or raw == "":
        return NOTHING
    if field is None:
        return str(raw)
    if field.attname == "password":
        return "(hidden)"
    if isinstance(field, models.BooleanField):
        return "yes" if raw else "no"
    if isinstance(field, models.ForeignKey):
        target = field.related_model._base_manager.filter(pk=raw).first()
        return str(target) if target is not None else f"no longer exists (id {raw})"
    if isinstance(field, models.DecimalField):
        try:
            return f"{Decimal(str(raw)):.2f}".replace(".", ",")
        except InvalidOperation:
            return str(raw)
    if isinstance(field, models.DateTimeField):
        moment = parse_datetime(str(raw))
        return timezone.localtime(moment).strftime(f"{DATE} %H:%M") if moment else str(raw)
    if isinstance(field, models.DateField):
        day = parse_date(str(raw))
        return day.strftime(DATE) if day else str(raw)
    if isinstance(field, models.UUIDField):
        return str(raw)[:8]
    return str(raw)


def label(field, attname):
    return str(field.verbose_name) if field is not None else attname


def lines(entry):
    """A headline, then what changed (or a deleted record's last values)."""
    when = timezone.localtime(entry.at).strftime(f"{DATE} %H:%M:%S")
    where = f" ({entry.ip})" if entry.ip else ""
    who = entry.username or "system"
    if entry.action == entry.LOGIN_FAILED:
        return [f"#{entry.pk} {when}{where}: failed to log in as “{entry.username}”"]
    if entry.action in (entry.LOGIN, entry.LOGOUT):
        return [f"#{entry.pk} {when} {who}{where}: {entry.get_action_display()}"]

    what = model_name(entry.model)
    head = f"#{entry.pk} {when} {who}{where}: {entry.get_action_display()} {what}"
    head += f" {entry.object_id}" if entry.object_id else ""
    head += f": {entry.object_repr}" if entry.object_repr else ""
    fields = fields_of(entry.model)
    out = [head]
    if entry.action == entry.UPDATE:
        for name, (old, new) in entry.changes().items():
            field = fields.get(name)
            if name == "password":
                out.append("    password: changed")
            else:
                out.append(f"    {label(field, name)}: {value(field, old)} → {value(field, new)}")
    elif entry.action == entry.DELETE and entry.before:
        out.append("    last values:")
        for name, raw in entry.before.items():
            if name not in HIDDEN and raw not in (None, ""):
                field = fields.get(name)
                out.append(f"      {label(field, name)}: {value(field, raw)}")
    return out
