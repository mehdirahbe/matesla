from django.conf import settings
from django.db import models
from django.utils import timezone


class TeslaOAuthPending(models.Model):
    """
    Survives the round-trip to auth.tesla.com even if the Django session cookie
    is dropped (common after external OAuth redirects).
    """

    state = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tesla_oauth_pendings",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Tesla OAuth pending state"
        verbose_name_plural = "Tesla OAuth pending states"

    def __str__(self):
        return f"OAuthPending({self.state[:8]}… user={self.user_id})"

    @classmethod
    def purge_expired(cls, max_age_minutes=30):
        cutoff = timezone.now() - timezone.timedelta(minutes=max_age_minutes)
        cls.objects.filter(created_at__lt=cutoff).delete()
