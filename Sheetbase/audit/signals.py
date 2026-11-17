"""Hooks that feed the audit log: every save and delete of the models in settings.AUDIT_MODELS, and logins."""

from django.apps import apps
from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_delete, post_save, pre_delete, pre_save

from .log import record, snapshot, stored
from .models import Entry


def audited_models():
    return [apps.get_model(label) for label in settings.AUDIT_MODELS]


def before_save(sender, instance, raw=False, **kwargs):
    if not raw and not instance._state.adding and instance.pk is not None:
        instance._audit_before = stored(instance)


def after_save(sender, instance, created, raw=False, **kwargs):
    if raw:  # loading fixtures
        return
    before = None if created else getattr(instance, "_audit_before", None)
    after = stored(instance)
    if before is None:
        record(Entry.CREATE, instance, after=after)
    elif before != after:  # e.g. a login only touches last_login, which isn't logged as a change
        record(Entry.UPDATE, instance, before=before, after=after)


def before_delete(sender, instance, **kwargs):
    instance._audit_before = stored(instance) or snapshot(instance)


def after_delete(sender, instance, **kwargs):
    record(Entry.DELETE, instance, before=instance._audit_before)


def logged_in(sender, request, user, **kwargs):
    record(Entry.LOGIN, user, request=request, user=user)


def logged_out(sender, request, user, **kwargs):
    if user is not None:
        record(Entry.LOGOUT, user, request=request, user=user)


def login_failed(sender, credentials, request=None, **kwargs):
    # Only who they tried to log in as – never the password.
    tried = credentials.get("username") or credentials.get("email") or ""
    record(Entry.LOGIN_FAILED, request=request, username=str(tried)[:150], model=settings.AUTH_USER_MODEL.lower())


def connect():
    for model in audited_models():
        uid = f"audit-{model._meta.label_lower}"
        pre_save.connect(before_save, sender=model, dispatch_uid=f"{uid}-pre_save")
        post_save.connect(after_save, sender=model, dispatch_uid=f"{uid}-post_save")
        pre_delete.connect(before_delete, sender=model, dispatch_uid=f"{uid}-pre_delete")
        post_delete.connect(after_delete, sender=model, dispatch_uid=f"{uid}-post_delete")
    user_logged_in.connect(logged_in, dispatch_uid="audit-login")
    user_logged_out.connect(logged_out, dispatch_uid="audit-logout")
    user_login_failed.connect(login_failed, dispatch_uid="audit-login-failed")
