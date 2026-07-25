from django.db import models


class TeslaAppSettings(models.Model):
    """
    Singleton: credentials of the Tesla developer application (MyRobotCar).
    Stored in DB so a personal install does not need .env files.
    """

    client_id = models.CharField(max_length=128)
    client_secret = models.CharField(max_length=256)
    redirect_uri = models.URLField(
        max_length=512,
        default="http://localhost:8001/oauth/callback",
        help_text="Must match exactly the redirect URI registered on developer.tesla.com",
    )
    # Fleet API audience / base URL for the user's region
    api_base = models.URLField(
        max_length=256,
        default="https://fleet-api.prd.eu.vn.cloud.tesla.com",
        help_text="EU: fleet-api.prd.eu… — NA: fleet-api.prd.na…",
    )
    # Public domain used for partner register (NOT localhost). Host public key there.
    partner_domain = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Bare domain e.g. robotcar.example.com — must serve the public key over HTTPS",
    )
    partner_registered = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Tesla app settings"
        verbose_name_plural = "Tesla app settings"

    def __str__(self):
        return f"TeslaAppSettings({self.client_id[:8]}…)"

    @classmethod
    def get_solo(cls):
        return cls.objects.order_by("pk").first()

    @classmethod
    def is_configured(cls):
        obj = cls.get_solo()
        return bool(obj and obj.client_id and obj.client_secret)
