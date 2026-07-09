---
hide:
  - navigation
  - toc
---

<div class="euroflood-hero" markdown>

# EuroFlood

**Query Europe's satellite flood-depth maps as GeoDataFrames — in a few lines.**

Discover *where and when* floods happened across Europe from a published index (zero
config), then download only the depth rasters you want — no 35 GB bulk archive, no setup.

[![License: MIT](https://img.shields.io/badge/license-MIT-0277BD.svg)](https://github.com/cisgroup/euroflood/blob/main/LICENSE)
[![CI](https://github.com/cisgroup/euroflood/actions/workflows/ci.yml/badge.svg)](https://github.com/cisgroup/euroflood/actions/workflows/ci.yml)
![Python 3.13](https://img.shields.io/badge/python-3.13-0277BD.svg)

![Flood recurrence over Zutphen on the IJssel](images/hero.png#only-light){ width="720" }
![Flood recurrence over Zutphen on the IJssel](images/hero-dark.png#only-dark){ width="720" }

[Get started](getting-started.md){ .md-button .md-button--primary }
[Tutorials](tutorials/README.md){ .md-button }
[API reference](reference/index.md){ .md-button }

</div>

## Install

=== "pip"

    ```bash
    pip install "euroflood[viz]"
    ```

=== "uv"

    ```bash
    uv add "euroflood[viz]"
    ```

## Three lines

```python
import euroflood as ef

cat = ef.floods("Zutphen, Netherlands")  # discover — a GeoDataFrame, one row per flood
cat.plot()                                # visualize — recurrence heatmap, no download
cat.download().stats()                    # measure — fetch depth rasters + per-event stats
```

Zero configuration: the published index is read remotely and cached on first use.

## Why EuroFlood

<div class="grid cards" markdown>

-   :material-earth:{ .lg .middle } __One API, all of Europe__

    ---

    Continental coverage from ~3,280 Sentinel-1 flood-depth maps (2015–2024), indexed
    so a query streams a few MB — never the 35 GB archive.

    [:octicons-arrow-right-24: How it works](concepts.md)

-   :material-table:{ .lg .middle } __GeoDataFrames, not tiles__

    ---

    Every query returns a GeoPandas `GeoDataFrame` — filter, clip, join, and export
    with the tools you already use.

    [:octicons-arrow-right-24: Discover & filter](tutorials/02_discover_and_filter.ipynb)

-   :material-map:{ .lg .middle } __Visualize without downloading__

    ---

    Recurrence heatmaps, per-event footprints, and interactive Leaflet maps straight
    from the index — no rasters fetched.

    [:octicons-arrow-right-24: Visualize](tutorials/03_visualize.ipynb)

-   :material-waves:{ .lg .middle } __Global hazard maps__

    ---

    The same API queries modelled **CEMS-GLOFAS** return-period depth (10–500 yr):
    `ef.hazard("Zutphen", return_period=100)`.

    [:octicons-arrow-right-24: Hazard](tutorials/05_hazard.ipynb)

-   :material-console:{ .lg .middle } __CLI & offline__

    ---

    Every query is also a `euroflood` command; mirror the index once for fully
    offline / HPC use.

    [:octicons-arrow-right-24: CLI & configuration](tutorials/07_cli_and_config.ipynb)

-   :material-book-check:{ .lg .middle } __Cited & reproducible__

    ---

    A versioned, checksummed index bundle with a manifest — pin a version, cite the
    source, reproduce a result.

    [:octicons-arrow-right-24: Get started](getting-started.md)

</div>

## Learn by doing

The **[tutorials](tutorials/README.md)** take you from a first query to hazard maps and
quantitative analysis — runnable notebooks that follow one place (Zutphen, on the
IJssel) throughout.

---

EuroFlood **code** is MIT-licensed. The **index and flood-depth maps** derive from the
JRC / Copernicus CEMS-EFAS dataset (CC-BY-4.0) — please cite Betterle & Salamon (2025).
Developed at Princeton University (Complex Infrastructure Systems Group).
