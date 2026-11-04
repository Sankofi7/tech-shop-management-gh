from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class AuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Audit history cannot be changed.")

    def delete(self):
        raise ValidationError("Audit history cannot be deleted.")


class AuditEvent(models.Model):
    action = models.CharField(max_length=50)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="audit_events")
    object_type = models.CharField(max_length=50)
    object_reference = models.CharField(max_length=120)
    occurred_at = models.DateTimeField(auto_now_add=True)
    details = models.JSONField(default=dict, blank=True)

    objects = AuditEventQuerySet.as_manager()

    class Meta:
        ordering = ("-occurred_at", "-pk")

    def __str__(self):
        return f"{self.action} {self.object_type} {self.object_reference}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Audit events are append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit history cannot be deleted.")


def record_audit_event(*, action, actor, object_type, object_reference, details=None):
    return AuditEvent.objects.create(
        action=action,
        actor=actor,
        object_type=object_type,
        object_reference=str(object_reference),
        details=details or {},
    )
