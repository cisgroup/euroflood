"""Generate the documentation images from real EuroFlood output (Zutphen, IJssel).

Maintainer tool — run once to (re)generate the committed hero + figure images under
``docs/images/``. Needs network (streams the published index, downloads a few depth
rasters, and fetches the grayscale/dark basemap tiles via contextily).

    uv run python scripts/make_doc_images.py

Every figure is a real query result with a grayscale (or dark) basemap behind it —
"flooding in front of a map" — so the docs show the actual system, not a placeholder.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless backend before importing pyplot

import matplotlib.pyplot as plt

import euroflood as ef

warnings.filterwarnings("ignore")

_ROOT = Path(__file__).resolve().parent.parent
OUT = _ROOT / "docs" / "images"
# Gallery thumbnails live under examples/ so the tutorials README resolves them both
# on GitHub and on the docs site (served via the docs/tutorials -> examples symlink).
THUMBS = _ROOT / "examples" / "thumbs"
PLACE = "Zutphen, Netherlands"


def _save(ax, name: str, *, dpi: int = 140) -> None:
    """Save a clean, frameless figure (keeps the colorbar legend, drops the title)."""
    ax.set_axis_off()  # drop lon/lat ticks — a clean map, not a chart
    ax.set_title("")  # captions live in the Markdown, not baked into the image
    ax.figure.savefig(OUT / name, dpi=dpi, bbox_inches="tight", pad_inches=0.04)
    plt.close(ax.figure)
    print(f"  wrote images/{name}  ({(OUT / name).stat().st_size / 1024:.0f} KB)")


def _thumb_ax(ax, name: str, *, is_map: bool = True) -> None:
    """Save a plot as a gallery thumbnail.

    For maps, strip the ticks *and* the colorbar so the map fills the card (a tiny
    thumbnail needs no readable scale).
    """
    if is_map:
        for extra in list(ax.figure.axes):
            if extra is not ax:
                extra.remove()  # drop the colorbar / legend axes
        ax.set_axis_off()
    ax.set_title("")
    THUMBS.mkdir(parents=True, exist_ok=True)
    ax.figure.savefig(THUMBS / name, dpi=95, bbox_inches="tight", pad_inches=0.02)
    plt.close(ax.figure)
    print(f"  wrote thumbs/{name}")


def main() -> None:
    """Generate all documentation figures + gallery thumbnails from live queries."""
    OUT.mkdir(parents=True, exist_ok=True)
    ef.settings.output_dir = Path("/tmp/euroflood_doc_out")  # scratch, not the repo

    print("recurrence + footprints (index-only)…")
    cat = ef.floods(PLACE, shape="bbox")
    _save(cat.plot(basemap=True, tiles="grayscale"), "recurrence.png")
    _save(cat.plot(footprints=True, basemap=True, tiles="grayscale"), "footprints.png")

    print("depth (downloading the largest events)…")
    dl = cat.sort_values("area_km2", ascending=False).head(8).download()
    _save(dl.plot(depth=True, basemap=True, tiles="grayscale"), "depth.png")

    print("hazard (GLOFAS 100-yr)…")
    haz = ef.hazard(PLACE, return_period=100, shape="bbox").download()
    _save(haz.plot(depth=True, basemap=True, tiles="grayscale"), "hazard.png")

    print("hero (recurrence — the signature view — light + dark)…")
    _save(cat.plot(basemap=True, tiles="grayscale"), "hero.png", dpi=170)
    # Keep the packaged product-card hero (uploaded to Source Cooperative at publish time,
    # via euroflood.pipelines) in sync with the docs hero.
    shutil.copy(
        OUT / "hero.png", _ROOT / "src" / "euroflood" / "pipelines" / "product_hero.png"
    )
    with plt.style.context("dark_background"):
        _save(cat.plot(basemap=True, tiles="dark"), "hero-dark.png", dpi=170)

    print("gallery thumbnails (clean colorbar-free maps, one per tutorial)…")
    minx, miny, maxx, maxy = cat.total_bounds  # frame every map to the same ROI

    def framed(ax: object) -> object:
        """Clip an axes to the shared ROI so every map thumbnail matches."""
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)
        return ax

    _thumb_ax(framed(cat.plot(basemap=True, tiles="grayscale")), "01_quickstart.png")
    _thumb_ax(  # 02: recurrence in a different palette
        framed(cat.plot(basemap=True, tiles="grayscale", cmap="viridis")),
        "02_discover.png",
    )
    _thumb_ax(  # 03: per-event footprints — no legend box, full ROI
        framed(
            cat.plot(footprints=True, basemap=True, tiles="grayscale", legend=False)
        ),
        "03_visualize.png",
    )
    # 04: historic depth is a thin corridor — saturate (vmax=2 m) so it reads boldly.
    _thumb_ax(
        framed(dl.plot(depth=True, basemap=True, tiles="grayscale", vmax=2)),
        "04_download.png",
    )
    # 05: the 100-yr hazard inundates the whole valley — keep the natural gradient
    # (auto vmax) so the shallow floodplain → deep channel reads, not a flat blob.
    _thumb_ax(
        framed(haz.plot(depth=True, basemap=True, tiles="grayscale")),
        "05_hazard.png",
    )
    with plt.style.context("dark_background"):  # 07: recurrence on a dark basemap
        _thumb_ax(framed(cat.plot(basemap=True, tiles="dark")), "07_cli.png")
    # 06: the quantitative bar chart (keeps its axes + labels)
    stats = dl.stats().sort_values("date")
    ax = stats.plot.bar(x="date", y="max_depth_m", legend=False, color="#0277bd")
    ax.set_ylabel("max depth (m)")
    ax.set_xlabel("")
    _thumb_ax(ax, "06_analysis.png", is_map=False)
    print("Done.")


if __name__ == "__main__":
    main()
