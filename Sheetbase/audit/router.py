"""Puts the audit log in its own database (the "audit" alias) and nothing else there."""

DB = "audit"


class AuditRouter:
    def db_for_read(self, model, **hints):
        return DB if model._meta.app_label == "audit" else None

    def db_for_write(self, model, **hints):
        return DB if model._meta.app_label == "audit" else None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "audit":
            return db == DB
        return db != DB
