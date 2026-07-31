"""
Shared matplotlib styling for MaTesla graphs (dark UI + two resolutions).

size:
  - thumb  → compact grid cards (lower DPI / smaller figure)
  - full   → lightbox or direct URL (sharper, larger)

All graph endpoints should build figures through this module so colors and
typography stay aligned with static/css/matesla.css.
"""

from __future__ import annotations

import io

from django.http import HttpResponse
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

# Palette aligned with static/css/matesla.css dark tokens
BG = "#0b1220"
AXES_BG = "#101a2b"
TEXT = "#eef4ff"
MUTED = "#a8bcd6"
GRID = "#2a3f5c"
SPINE = "#3a5070"
ACCENT = "#4db4ff"
ACCENT_SOFT = "#2f8fff"
ENERGY = "#2fd4a1"
CYAN = "#3ec9e0"
WARM = "#f0b429"
DANGER = "#ff5c6a"

# Date series: min / avg / max — deliberately far apart (not three cool tones).
# Min = cool blue, Avg = amber (high contrast), Max = coral red.
SERIES_COLORS = (ACCENT, WARM, DANGER)

# Scatter + linear regression
SCATTER_FACE = ACCENT
SCATTER_EDGE = "#0b1220"
FIT_LINEAR = DANGER
BAR_FACE = ACCENT_SOFT

# Typography / geometry presets for the two display sizes used by the UI
GRAPH_SIZES = {
    "thumb": {
        "figsize": (7.0, 3.05),
        "figsize_bar": (7.0, 2.35),
        "dpi": 112,
        "markersize": 2.2,
        "linewidth": 1.45,
        "scatter_size": 12,
        "title_size": 11,
        "label_size": 8.5,
        "tick_size": 7.5,
        "legend_size": 7.5,
        "title_pad": 10,
        "spine_width": 0.8,
    },
    "full": {
        "figsize": (12.0, 5.15),
        "figsize_bar": (12.0, 3.15),
        "dpi": 144,
        "markersize": 3.6,
        "linewidth": 1.9,
        "scatter_size": 22,
        "title_size": 14,
        "label_size": 10.5,
        "tick_size": 9.5,
        "legend_size": 9.5,
        "title_pad": 14,
        "spine_width": 0.9,
    },
}


def parse_graph_size(raw, default: str = "full") -> str:
    """Normalize ?size= query values to 'thumb' or 'full'."""
    if raw is None:
        return default
    key = str(raw).strip().lower()
    if key in ("thumb", "small", "sm", "grid", "card"):
        return "thumb"
    if key in ("full", "large", "lg", "hi", "lightbox"):
        return "full"
    return default


def graph_size_from_request(request, default: str = "full") -> str:
    """Read ?size=thumb|full (default full for bookmarked direct URLs)."""
    return parse_graph_size(request.GET.get("size"), default=default)


def size_config(size: str) -> dict:
    """Return the style dict for a graph size key."""
    return GRAPH_SIZES["thumb" if size == "thumb" else "full"]


def make_figure(size: str = "full", *, bar: bool = False) -> tuple[Figure, dict]:
    """
    Create a dark-themed Figure and its style config.

    bar=True uses a shorter figure height suited to histogram cards.
    Returns (figure, style_config) so callers never invent ad-hoc DPI/fonts.
    """
    style_config = size_config(size)
    figsize = style_config["figsize_bar"] if bar else style_config["figsize"]
    figure = Figure(
        figsize=figsize,
        dpi=style_config["dpi"],
        facecolor=BG,
        edgecolor=BG,
        tight_layout={"pad": 0.55 if size == "thumb" else 0.9},
    )
    return figure, style_config


def style_axes(axes, style_config: dict) -> None:
    """Apply dark grid, muted ticks, and plain Y formatting to an Axes."""
    axes.set_facecolor(AXES_BG)
    axes.tick_params(
        colors=MUTED,
        labelsize=style_config["tick_size"],
        length=3.5,
        width=0.7,
    )
    axes.xaxis.label.set_color(MUTED)
    axes.yaxis.label.set_color(MUTED)
    axes.title.set_color(TEXT)
    for spine in axes.spines.values():
        spine.set_color(SPINE)
        spine.set_linewidth(style_config["spine_width"])
    axes.grid(True, color=GRID, linewidth=0.7, alpha=0.55, linestyle="-")
    axes.set_axisbelow(True)
    axes.ticklabel_format(axis="y", useOffset=False, style="plain")


def style_suptitle(figure: Figure, title, style_config: dict) -> None:
    """Set a bold white figure title when present."""
    if not title:
        return
    figure.suptitle(
        title,
        color=TEXT,
        fontsize=style_config["title_size"],
        fontweight="bold",
        y=0.98,
    )


def style_legend(axes, style_config: dict):
    """Dark legend panel matching the rest of the UI chrome."""
    legend = axes.legend(
        facecolor="#162338",
        edgecolor=SPINE,
        labelcolor=TEXT,
        fontsize=style_config["legend_size"],
        framealpha=0.92,
        borderpad=0.5,
    )
    if legend is not None:
        legend.get_frame().set_linewidth(0.8)
    return legend


def finish_figure(figure: Figure, axes, title, style_config: dict) -> None:
    """Apply axes + title styling and a safe tight_layout."""
    style_axes(axes, style_config)
    style_suptitle(figure, title, style_config)
    try:
        figure.tight_layout(rect=(0.02, 0.02, 0.98, 0.92))
    except Exception:
        pass


def render_png(figure: Figure, size: str = "full") -> HttpResponse:
    """Encode a figure as PNG and free matplotlib resources."""
    style_config = size_config(size)
    buffer = io.BytesIO()
    canvas = FigureCanvasAgg(figure)
    canvas.print_png(buffer)
    png_bytes = buffer.getvalue()
    figure.clear()
    response = HttpResponse(png_bytes, content_type="image/png")
    response["Content-Length"] = str(len(png_bytes))
    # Short private browser cache; params include size so thumb/full stay distinct
    response["Cache-Control"] = "private, max-age=120"
    response["X-MaTesla-Graph-Size"] = "thumb" if size == "thumb" else "full"
    response["X-MaTesla-Graph-Dpi"] = str(style_config["dpi"])
    return response
