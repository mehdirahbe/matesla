"""
Shared matplotlib styling for MaTesla graphs (dark UI + two resolutions).

size:
  - thumb  → compact grid cards (lower DPI / smaller figure)
  - full   → lightbox or direct URL (sharper, larger)
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

# Date series: min / avg / max — deliberately far apart (not 3 cool tones).
# Min = cool blue, Avg = amber (high contrast), Max = coral red.
SERIES_COLORS = (ACCENT, WARM, DANGER)

# Scatter + regression
SCATTER_FACE = ACCENT
SCATTER_EDGE = "#0b1220"
FIT_QUAD = WARM
FIT_LINEAR = DANGER
BAR_FACE = ACCENT_SOFT

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
    return GRAPH_SIZES["thumb" if size == "thumb" else "full"]


def make_figure(size: str = "full", *, bar: bool = False) -> tuple[Figure, dict]:
    cfg = size_config(size)
    figsize = cfg["figsize_bar"] if bar else cfg["figsize"]
    fig = Figure(
        figsize=figsize,
        dpi=cfg["dpi"],
        facecolor=BG,
        edgecolor=BG,
        tight_layout={"pad": 0.55 if size == "thumb" else 0.9},
    )
    return fig, cfg


def style_axes(ax, cfg: dict) -> None:
    ax.set_facecolor(AXES_BG)
    ax.tick_params(colors=MUTED, labelsize=cfg["tick_size"], length=3.5, width=0.7)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.title.set_color(TEXT)
    for spine in ax.spines.values():
        spine.set_color(SPINE)
        spine.set_linewidth(cfg["spine_width"])
    ax.grid(True, color=GRID, linewidth=0.7, alpha=0.55, linestyle="-")
    ax.set_axisbelow(True)
    ax.ticklabel_format(axis="y", useOffset=False, style="plain")


def style_suptitle(fig: Figure, title, cfg: dict) -> None:
    if not title:
        return
    fig.suptitle(
        title,
        color=TEXT,
        fontsize=cfg["title_size"],
        fontweight="bold",
        y=0.98,
    )


def style_legend(ax, cfg: dict):
    leg = ax.legend(
        facecolor="#162338",
        edgecolor=SPINE,
        labelcolor=TEXT,
        fontsize=cfg["legend_size"],
        framealpha=0.92,
        borderpad=0.5,
    )
    if leg is not None:
        leg.get_frame().set_linewidth(0.8)
    return leg


def finish_figure(fig: Figure, ax, title, cfg: dict) -> None:
    style_axes(ax, cfg)
    style_suptitle(fig, title, cfg)
    try:
        fig.tight_layout(rect=(0.02, 0.02, 0.98, 0.92))
    except Exception:
        pass


def render_png(fig: Figure, size: str = "full") -> HttpResponse:
    """Encode figure as PNG and free it."""
    cfg = size_config(size)
    buf = io.BytesIO()
    canvas = FigureCanvasAgg(fig)
    canvas.print_png(buf)
    data = buf.getvalue()
    fig.clear()
    response = HttpResponse(data, content_type="image/png")
    response["Content-Length"] = str(len(data))
    # Hint for intermediate caches (params include size)
    response["Cache-Control"] = "private, max-age=120"
    response["X-MaTesla-Graph-Size"] = "thumb" if size == "thumb" else "full"
    response["X-MaTesla-Graph-Dpi"] = str(cfg["dpi"])
    return response
