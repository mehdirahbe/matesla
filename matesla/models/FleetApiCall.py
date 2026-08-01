"""
Log of billable-ish Fleet HTTP calls for cost graphs.

Only vehicle_data is recorded for now (the expensive Data category).
List /vehicles stays unlogged here — inventory/status, not the cost chart.
"""

from __future__ import annotations

from django.db import models
from django.utils import timezone

from matesla.models.VinHash import HashTheVin

KIND_VEHICLE_DATA = "vehicle_data"
KIND_CHOICES = (
    (KIND_VEHICLE_DATA, "vehicle_data"),
)

SOURCE_CAPTURE = "capture"
SOURCE_STATUS = "status_page"
SOURCE_OTHER = "other"


class FleetApiCall(models.Model):
    """One Fleet API HTTP attempt that can affect Data billing."""

    at = models.DateTimeField(db_index=True, default=timezone.now)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES, default=KIND_VEHICLE_DATA)
    source = models.CharField(max_length=32, blank=True, default="")
    vin = models.TextField(null=True, blank=True, db_index=True)
    hashedVin = models.TextField(null=True, blank=True, db_index=True)
    user_id = models.IntegerField(null=True, blank=True, db_index=True)
    http_status = models.IntegerField(null=True, blank=True)
    # Tesla: status codes below 500 are billable usage
    billable = models.BooleanField(default=False)
    # Soft error label (FR/ops), never huge
    detail = models.CharField(max_length=240, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["hashedVin", "at"]),
            models.Index(fields=["kind", "billable", "at"]),
        ]

    def __str__(self):
        return (
            f"FleetApiCall({self.kind} status={self.http_status} "
            f"billable={self.billable} at={self.at})"
        )


def record_vehicle_data_call(
    *,
    http_status: int | None,
    vin: str | None = None,
    hashed_vin: str | None = None,
    user_id: int | None = None,
    source: str = SOURCE_OTHER,
    detail: str = "",
    when=None,
) -> FleetApiCall | None:
    """
    Persist one vehicle_data attempt. Never raises (cost logging must not break capture).
    """
    try:
        vin_s = (vin or "").strip() or None
        hash_s = (hashed_vin or "").strip() or None
        if not hash_s and vin_s:
            hash_s = HashTheVin(vin_s)
        billable = http_status is not None and int(http_status) < 500
        return FleetApiCall.objects.create(
            at=when or timezone.now(),
            kind=KIND_VEHICLE_DATA,
            source=(source or "")[:32],
            vin=vin_s,
            hashedVin=hash_s,
            user_id=user_id,
            http_status=http_status,
            billable=billable,
            detail=(detail or "")[:240],
        )
    except Exception:
        # Logging must never break Fleet polling
        return None
