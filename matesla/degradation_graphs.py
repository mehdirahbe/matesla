"""
Shared PNG / scatter helpers for personal battery-degradation graphs.

Moved out of the former anonymised fleet-stats app so personalstats can
keep degradation charts without the public fleet UI.
"""

from __future__ import annotations

import csv
from collections import defaultdict

import numpy as np
from django.db import connection
from django.db.models import Q
from django.http import HttpResponse

from matesla.graphstyle import (
    FIT_LINEAR,
    SCATTER_EDGE,
    SCATTER_FACE,
    finish_figure,
    make_figure,
    render_png,
    style_legend,
)

# Scatter fits are much noisier at low SoC (range extrapolation).
# Prefer ≥ 80 % (Tesla’s recommended daily charge limit); active charging excluded.
DEGRADATION_SCATTER_MIN_SOC = 80.0
# If a car rarely reaches 80 %, fall back so the graph is not empty.
DEGRADATION_SCATTER_FALLBACK_SOC = 75.0
DEGRADATION_SCATTER_MIN_POINTS = 15


def GeneratePngFromGraph(figure, size="full"):
    """Encode a matplotlib figure as an HTTP PNG response."""
    return render_png(figure, size=size)


def PrepareCSVFromQuery(query):
    """Run raw SQL and stream the result as a CSV HttpResponse."""
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="export.csv"'
    writer = csv.writer(response)
    with connection.cursor() as cursor:
        cursor.execute(query)
        title = [col[0] for col in cursor.description]
        writer.writerow(title)
        for row in cursor.fetchall():
            writer.writerow([str(field) for field in row])
    return response


def _soc_for_scatter(entry) -> float | None:
    """Prefer usable_battery_level, else battery_level, as float percent."""
    if not isinstance(entry, dict):
        entry = entry.__dict__
    usable = entry.get("usable_battery_level")
    if usable is not None:
        try:
            return float(usable)
        except (TypeError, ValueError):
            return None
    battery_level = entry.get("battery_level")
    if battery_level is None:
        return None
    try:
        return float(battery_level)
    except (TypeError, ValueError):
        return None


def high_soc_scatter_q(minimum_soc: float = DEGRADATION_SCATTER_MIN_SOC):
    """
    ORM filter: high SoC and not actively charging.

    Charging rows couple range and SoC poorly and create vertical noise clouds.
    """
    high_soc = Q(usable_battery_level__gte=minimum_soc) | (
        Q(usable_battery_level__isnull=True) & Q(battery_level__gte=minimum_soc)
    )
    return high_soc & ~Q(charging_state="Charging")


def degradation_scatter_queryset(queryset):
    """Prefer strict high-SoC filter; fall back if too few points for a trend."""
    strict = queryset.filter(high_soc_scatter_q())
    # [:N].count() avoids a full table COUNT on huge histories
    if (
        strict[:DEGRADATION_SCATTER_MIN_POINTS].count()
        >= DEGRADATION_SCATTER_MIN_POINTS
    ):
        return strict
    return queryset.filter(
        high_soc_scatter_q(minimum_soc=DEGRADATION_SCATTER_FALLBACK_SOC)
    )


def _median(values):
    """Median of a non-empty numeric list (None if empty)."""
    sorted_values = sorted(values)
    count = len(sorted_values)
    if count == 0:
        return None
    middle = count // 2
    if count % 2:
        return sorted_values[middle]
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def aggregate_scatter_daily_median(x_values, y_values, sample_dates):
    """
    Collapse many same-day snapshots into one median (X, Y) point per civil day.

    TeslaFi-style histories poll often; the vertical cloud is mostly same-day
    BMS jitter, not real degradation change. Daily median makes the trend readable.
    """
    if (
        not sample_dates
        or len(sample_dates) != len(x_values)
        or len(x_values) == 0
    ):
        return x_values, y_values

    per_day = defaultdict(lambda: ([], []))
    for sample_date, x_value, y_value in zip(sample_dates, x_values, y_values):
        if sample_date is None or x_value is None or y_value is None:
            continue
        civil_day = (
            sample_date.date() if hasattr(sample_date, "date") else sample_date
        )
        try:
            per_day[civil_day][0].append(float(x_value))
            per_day[civil_day][1].append(float(y_value))
        except (TypeError, ValueError):
            continue
    if not per_day:
        return x_values, y_values
    median_x_values, median_y_values = [], []
    for civil_day in sorted(per_day.keys()):
        day_x_values, day_y_values = per_day[civil_day]
        median_x = _median(day_x_values)
        median_y = _median(day_y_values)
        if median_x is None or median_y is None:
            continue
        median_x_values.append(median_x)
        median_y_values.append(median_y)
    return median_x_values, median_y_values


def GetXandYFromBatteryDegradResult(
    results, x_field, *, daily_median=True, minimum_soc=None
):
    """
    Extract scatter series from ORM rows: X = x_field, Y = battery_degradation.

    Filters high SoC and non-charging; optionally collapses to daily medians.
    """
    if minimum_soc is None:
        minimum_soc = DEGRADATION_SCATTER_FALLBACK_SOC
    x_values = []
    y_values = []
    sample_dates = []
    for entry in results:
        raw = entry if isinstance(entry, dict) else entry.__dict__
        if raw.get("battery_degradation") is None:
            continue
        if raw.get(x_field) is None:
            continue
        state_of_charge = _soc_for_scatter(raw)
        if state_of_charge is None or state_of_charge < minimum_soc:
            continue
        if raw.get("charging_state") == "Charging":
            continue
        x_values.append(raw[x_field])
        y_values.append(raw["battery_degradation"])
        sample_dates.append(raw.get("Date"))
    if daily_median:
        return aggregate_scatter_daily_median(x_values, y_values, sample_dates)
    return x_values, y_values


def FormatDouble2Decimals(value):
    """Two-decimal scientific notation, stripping useless e+00 exponents."""
    return "{:.2e}".format(value).replace("e+00", "")


def GenerateScatterGraph(x_values, y_values, title, size="full"):
    """
    Degradation scatter with a linear trend line (R² + slope per 10k miles).

    Quadratic fits misled on long histories, so we only show a linear polyfit.
    """
    figure, style_config = make_figure(size)
    axes = figure.subplots()
    if x_values is not None and y_values is not None and len(x_values) > 0:
        axes.scatter(
            x_values,
            y_values,
            s=style_config["scatter_size"],
            c=SCATTER_FACE,
            alpha=0.45,
            edgecolors=SCATTER_EDGE,
            linewidths=0.25,
            zorder=2,
        )
        if len(x_values) >= 2:
            sorted_unique_x = list(dict.fromkeys(x_values))
            sorted_unique_x.sort()
            x_array = np.asarray(x_values, dtype=float)
            y_array = np.asarray(y_values, dtype=float)
            linear_coefficients = np.polyfit(x_array, y_array, 1)
            linear_polynomial = np.poly1d(linear_coefficients)
            y_predicted = linear_polynomial(x_array)
            residual_sum_squares = float(np.sum((y_array - y_predicted) ** 2))
            total_sum_squares = float(
                np.sum((y_array - np.mean(y_array)) ** 2)
            )
            r_squared = (
                1.0 - residual_sum_squares / total_sum_squares
                if total_sum_squares > 0
                else float("nan")
            )
            slope_per_10k_miles = float(linear_coefficients[0]) * 10000.0
            r_squared_text = (
                f"{r_squared:.2f}" if r_squared == r_squared else "—"
            )
            trend_label = (
                f"R²={r_squared_text}  ·  {slope_per_10k_miles:+.2f} pt/10k mi\n"
                f"{FormatDouble2Decimals(linear_coefficients[0])}x+"
                f"{FormatDouble2Decimals(linear_coefficients[1])}"
            )
            axes.plot(
                sorted_unique_x,
                linear_polynomial(sorted_unique_x),
                "-",
                color=FIT_LINEAR,
                linewidth=style_config["linewidth"],
                label=trend_label,
                zorder=3,
            )
            style_legend(axes, style_config)
    finish_figure(figure, axes, title, style_config)
    return GeneratePngFromGraph(figure, size=size)
