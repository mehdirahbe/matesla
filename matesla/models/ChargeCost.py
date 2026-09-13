"""
User-configured charging places, tariffs, Elia spot cache, and persisted session costs.

Nothing here is vehicle- or address-specific: the owner enters places, roles,
periods and prices. See matesla.charge_cost for the pricing engine.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


ROLE_HOME = "home"
ROLE_WORK = "work"
PLACE_ROLE_CHOICES = (
    (ROLE_HOME, _("Home")),
    (ROLE_WORK, _("Work")),
)

TARIFF_FLAT = "flat"
TARIFF_DAY_NIGHT = "day_night"
TARIFF_DYNAMIC = "dynamic"
TARIFF_MODE_CHOICES = (
    (TARIFF_FLAT, _("Flat")),
    (TARIFF_DAY_NIGHT, _("Day / night")),
    (TARIFF_DYNAMIC, _("Dynamic (day-ahead + cents)")),
)

COST_PRICED = "priced"
COST_UNPRICED = "unpriced"
COST_PARTIAL = "partial"
COST_STATUS_CHOICES = (
    (COST_PRICED, _("Priced")),
    (COST_UNPRICED, _("Unpriced")),
    (COST_PARTIAL, _("Partial")),
)

RULE_HOME_FLAT = "home_flat"
RULE_HOME_DAY_NIGHT = "home_day_night"
RULE_HOME_DYNAMIC = "home_dynamic"
RULE_WORK = "work"
RULE_SUPERCHARGER_INVOICE = "supercharger_invoice"
RULE_SUPERCHARGER_RATE = "supercharger_rate"
RULE_PLACE = "place"
RULE_OTHER = "other"
RULE_UNPRICED = "unpriced"
RULE_CHOICES = (
    (RULE_HOME_FLAT, _("Home (flat)")),
    (RULE_HOME_DAY_NIGHT, _("Home (day / night)")),
    (RULE_HOME_DYNAMIC, _("Home (dynamic)")),
    (RULE_WORK, _("Work")),
    (RULE_PLACE, _("Named place")),
    (RULE_SUPERCHARGER_INVOICE, _("Tesla Supercharger invoice")),
    (RULE_SUPERCHARGER_RATE, _("Supercharger average rate")),
    (RULE_OTHER, _("Other chargers")),
    (RULE_UNPRICED, _("Unpriced")),
)


class ChargePlace(models.Model):
    """Named geofence (home, work, or any labelled spot). Role is per vehicle."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="charge_places",
    )
    name = models.CharField(max_length=128)
    latitude = models.FloatField()
    longitude = models.FloatField()
    radius_m = models.PositiveIntegerField(default=150)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return self.name


class VehiclePlaceRole(models.Model):
    """
    This vehicle treats this place as home or work during [valid_from, valid_to].

    valid_to is inclusive; null means still active.
    """

    hashed_vin = models.CharField(max_length=64, db_index=True)
    place = models.ForeignKey(
        ChargePlace,
        on_delete=models.CASCADE,
        related_name="vehicle_roles",
    )
    role = models.CharField(max_length=16, choices=PLACE_ROLE_CHOICES)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["hashed_vin", "role", "valid_from"]),
        ]

    def __str__(self):
        return f"{self.hashed_vin[:8]}… {self.role} @ {self.place_id}"


class PlaceTariffPeriod(models.Model):
    """Electricity tariff for a place over a date range (inclusive)."""

    place = models.ForeignKey(
        ChargePlace,
        on_delete=models.CASCADE,
        related_name="tariff_periods",
    )
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    mode = models.CharField(
        max_length=16,
        choices=TARIFF_MODE_CHOICES,
        default=TARIFF_FLAT,
    )
    flat_eur_per_kwh = models.FloatField(null=True, blank=True)
    day_eur_per_kwh = models.FloatField(null=True, blank=True)
    night_eur_per_kwh = models.FloatField(null=True, blank=True)
    night_start = models.TimeField(default="22:00:00")
    night_end = models.TimeField(default="07:00:00")
    # Cents added to Elia day-ahead €/kWh (spot/1000 + cents/100).
    dynamic_surcharge_cents = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["place", "valid_from"]),
        ]
        ordering = ["valid_from"]

    def __str__(self):
        return f"{self.place_id} {self.mode} {self.valid_from}"


class ChargeCostSettings(models.Model):
    """Per-user rates for Superchargers without invoice and other public chargers."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="charge_cost_settings",
    )
    other_eur_per_kwh = models.FloatField(
        null=True,
        blank=True,
        help_text=_("€/kWh for chargers that are not home, work, or a Supercharger"),
    )
    supercharger_eur_per_kwh = models.FloatField(
        null=True,
        blank=True,
        help_text=_("€/kWh when no Tesla Supercharger invoice is available"),
    )

    def __str__(self):
        return f"ChargeCostSettings(user={self.user_id})"


class DayAheadSpotPrice(models.Model):
    """Belgian day-ahead auction price (Elia), one MTU."""

    mtu_start = models.DateTimeField(db_index=True)
    resolution_minutes = models.PositiveSmallIntegerField()
    price_eur_mwh = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mtu_start", "resolution_minutes"],
                name="dayahead_spot_unique_mtu",
            ),
        ]
        indexes = [
            models.Index(fields=["mtu_start"]),
        ]

    def __str__(self):
        return f"{self.mtu_start} {self.price_eur_mwh} €/MWh"


class ChargeSessionCost(models.Model):
    """Persisted cost for one derived charge session (full plug-in, not a civil-day cut)."""

    hashed_vin = models.CharField(max_length=64, db_index=True)
    start = models.DateTimeField()
    end = models.DateTimeField()
    kwh = models.FloatField(null=True, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    place = models.ForeignKey(
        ChargePlace,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_costs",
    )
    rule = models.CharField(max_length=32, choices=RULE_CHOICES, default=RULE_UNPRICED)
    status = models.CharField(
        max_length=16,
        choices=COST_STATUS_CHOICES,
        default=COST_UNPRICED,
    )
    cost_eur = models.FloatField(null=True, blank=True)
    tesla_invoice_eur = models.FloatField(null=True, blank=True)
    tesla_session_id = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["hashed_vin", "start"],
                name="charge_session_cost_unique_start",
            ),
        ]
        indexes = [
            models.Index(fields=["hashed_vin", "start"]),
            models.Index(fields=["hashed_vin", "end"]),
        ]

    def __str__(self):
        return f"{self.hashed_vin[:8]}… {self.start} {self.rule} {self.cost_eur}"


class TeslaChargingInvoice(models.Model):
    """One Tesla-network charge from GET /api/1/dx/charging/history."""

    hashed_vin = models.CharField(max_length=64, db_index=True)
    tesla_session_id = models.CharField(max_length=64, unique=True)
    start = models.DateTimeField()
    end = models.DateTimeField()
    site_name = models.CharField(max_length=256, blank=True, default="")
    total_eur = models.FloatField()
    kwh = models.FloatField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True, default="EUR")
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["hashed_vin", "start"]),
        ]

    def __str__(self):
        return f"{self.tesla_session_id} {self.total_eur} {self.currency}"


class TeslaChargingHistorySync(models.Model):
    """Last successful Tesla charging-history pull for a vehicle."""

    hashed_vin = models.CharField(max_length=64, unique=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_from = models.DateTimeField(null=True, blank=True)
    last_to = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=240, blank=True, default="")

    def __str__(self):
        return f"TeslaChargingHistorySync({self.hashed_vin[:8]}…)"
