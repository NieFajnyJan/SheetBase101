from django.db import models, transaction

from .log import record, snapshot


class AuditedQuerySet(models.QuerySet):
    """update() changes rows without saving them one by one, so no save hook sees it. This one logs
    each row it changes, with its values before and after."""

    def update(self, **kwargs):
        with transaction.atomic(using=self.db):
            rows = list(self.model._base_manager.using(self.db).filter(pk__in=self.values("pk")))
            before = {row.pk: snapshot(row) for row in rows}
            count = super().update(**kwargs)
            for row in self.model._base_manager.using(self.db).filter(pk__in=before):
                after = snapshot(row)
                if after != before[row.pk]:
                    record("update", row, before=before[row.pk], after=after)
        return count

    update.alters_data = True
