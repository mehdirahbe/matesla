from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone


def get_sentinel_user():
    return get_user_model().objects.get_or_create(username="deleted")[0]


class TeslaToken(models.Model):
    """
    Per-user Fleet API OAuth credentials (account-level).
    Vehicles belong to TeslaVehicle — not stored here.
    """

    user_id = models.ForeignKey(
        get_user_model(), null=True, on_delete=models.SET(get_sentinel_user)
    )
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user_id"], name="one_tesla_token_per_user"),
        ]

    def __str__(self):
        return f"TeslaToken(user={self.user_id_id})"

    def is_access_token_expired(self, skew_seconds=120):
        if self.expires_at is None:
            return False
        return timezone.now() >= self.expires_at - timezone.timedelta(seconds=skew_seconds)


class TeslaVehicle(models.Model):
    """
    One row per vehicle accessible via a user's Tesla account.
    api_id is the Fleet API vehicle id used in URL paths.
    """

    user = models.ForeignKey(
        get_user_model(),
        on_delete=models.CASCADE,
        related_name="tesla_vehicles",
    )
    api_id = models.CharField(max_length=64)
    vin = models.CharField(max_length=32, blank=True, default="")
    display_name = models.CharField(max_length=128, blank=True, default="")
    state = models.CharField(max_length=32, blank=True, default="")  # online/asleep/…
    is_primary = models.BooleanField(
        default=False,
        help_text="Default vehicle when none is selected in session",
    )
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "api_id"], name="unique_vehicle_api_id_per_user"
            ),
        ]
        ordering = ["-is_primary", "display_name", "vin"]

    def __str__(self):
        label = self.display_name or self.vin or self.api_id
        return f"{label} ({self.api_id})"

    @property
    def label(self):
        if self.display_name and self.vin:
            return f"{self.display_name} ({self.vin[-6:]})"
        return self.display_name or self.vin or self.api_id
