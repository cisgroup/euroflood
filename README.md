# EuroFlood

[![CI](https://github.com/cisgroup/euroflood/actions/workflows/ci.yml/badge.svg)](https://github.com/cisgroup/euroflood/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/cisgroup/euroflood/branch/main/graph/badge.svg)](https://codecov.io/gh/cisgroup/euroflood)
[![PyPI](https://img.shields.io/pypi/v/euroflood.svg?color=0277bd)](https://pypi.org/project/euroflood/)
[![docs](https://img.shields.io/badge/docs-mkdocs--material-blue)](https://cisgroup.github.io/euroflood/)
[![Python](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/cisgroup/euroflood/blob/main/LICENSE)
[![DOI (software)](https://img.shields.io/badge/DOI%20(software)-10.5281%2Fzenodo.22837458-0277bd)](https://doi.org/10.5281/zenodo.22837458)
[![DOI (index)](https://img.shields.io/badge/DOI%20(index)-10.5281%2Fzenodo.21284459-0277bd)](https://doi.org/10.5281/zenodo.21284459)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**Query Europe's observed satellite flood-depth maps by place and time: lightweight, cloud-native, `pip`-installable.**

<p align="center">
  <a href="https://cisgroup.github.io/euroflood/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/cisgroup/euroflood/main/docs/images/hero-dark.png">
      <img src="https://raw.githubusercontent.com/cisgroup/euroflood/main/docs/images/hero.png" width="760" alt="EuroFlood: flood-recurrence map of Zutphen on the river IJssel, over a grayscale basemap" />
    </picture>
  </a>
</p>

<p align="center"><em>Flood-recurrence over Zutphen (river IJssel): how often each ~90&nbsp;m pixel flooded across a decade, straight from the index.</em></p>

EuroFlood is a data-access tool for the JRC / Copernicus **CEMS-EFAS Satellite-Derived
Flood Depth Maps for Europe**: ~3,610 observed, Sentinel-1-derived
flood-depth maps across Europe, 2015–2025. The source is published only as an un-indexed bulk
FTP archive; EuroFlood turns it into a **queryable index** so you can *discover* which flood
events touched a region (and when) and *extract* only the depth rasters you actually need.

It follows a **Discover → Extract** model:

- **Discover**: `floods("Zutphen, Netherlands")` streams a compact index (a Cloud-Optimized GeoTIFF read a
  window at a time via `/vsicurl`, plus a small sorted GeoParquet dictionary cached on first use)
  and returns a `GeoDataFrame` of matching flood events, transferring a few MB, never the whole
  archive.
- **Extract**: `.download("out/")` fetches and crops only the source depth GeoTIFFs for the
  events you selected.

It also exposes the global **CEMS-GLOFAS** modelled flood-hazard maps via `hazard()`.

## Install

```bash
pip install euroflood
```

## Quick start

```python
import euroflood as ef

# Discover observed flood events (place name, bbox, point+radius, ...)
cat = ef.floods("Zutphen, Netherlands")    # a GeoDataFrame
cat = ef.floods(bbox=(6.14, 52.09, 6.27, 52.17), start=2024, end=2024)
cat = ef.floods(nuts="NL22")               # a Eurostat NUTS region

# Extract: download + crop the depth rasters for the selected events
cat[cat["date"] >= "2024-01-01"].download("out/")

# Global modelled hazard (CEMS-GLOFAS return-period depth)
ef.hazard("Zutphen, Netherlands", return_period=100).download("hazard/")
```

Or from the command line:

```bash
euroflood floods "Zutphen, Netherlands"
euroflood hazard "Zutphen, Netherlands" -r 100 --download --out hazard/
```

> New to EuroFlood? Work through the runnable
> **[tutorials](https://cisgroup.github.io/euroflood/tutorials/)**: Quickstart →
> Discover & filter → Visualize → Download & measure → Hazard.

## Visualize (optional `[viz]` extra)

`pip install "euroflood[viz]"` adds plotting: flood-recurrence and per-event
footprints straight from the index (no download), plus the downloaded depth maps:

```python
cat = ef.floods("Zutphen, Netherlands")
cat.plot()                                 # flood-recurrence heatmap (shown at the top)
cat.footprints()                           # a GeoDataFrame of each event's extent (+ extent_km2)
cat.explore()                              # interactive map with per-region hover tooltips
cat.head(3).download().plot(depth=True)    # fetch + render the actual depth rasters
```

<p align="center">
  <img src="https://raw.githubusercontent.com/cisgroup/euroflood/main/docs/images/paper/fig-zutphen-multi.png" width="760" alt="A decade of flooding at Zutphen: per-cell recurrence, per-event depth, and observed depths on the modelled return-period curve" />
</p>

<p align="center"><em>A decade at Zutphen: per-cell recurrence, per-event depth, and observed depths against the modelled CEMS-GLOFAS return-period curve (see the <a href="https://cisgroup.github.io/euroflood/case-studies/02_zutphen_decade/">case study</a>).</em></p>

See the [Visualize tutorial](https://cisgroup.github.io/euroflood/tutorials/03_visualize/).

## Documentation

**Full docs: <https://cisgroup.github.io/euroflood/>**

- [Getting Started](https://cisgroup.github.io/euroflood/getting-started/):
  install and run your first query.
- [Tutorials](https://cisgroup.github.io/euroflood/tutorials/): runnable,
  progressive notebooks (Quickstart → Hazard).
- [Case studies](https://cisgroup.github.io/euroflood/case-studies/): real-world flood
  analyses on genuine events across Europe (Storm Boris, the Valencia DANA, and more).
- [Concepts](https://cisgroup.github.io/euroflood/concepts/): how the index
  works (the data model in one page).
- [API Reference](https://cisgroup.github.io/euroflood/reference/) ·
  [HPC runbook](https://cisgroup.github.io/euroflood/hpc-runbook/)

## Data source, attribution & license

EuroFlood **code** is licensed under the **MIT License**.

The **index and the underlying flood-depth maps** are derived from the JRC / Copernicus
CEMS-EFAS *Satellite-Derived Flood Depth Maps for Europe* and are licensed **CC-BY-4.0**.

For how to cite EuroFlood (the software, the index dataset, the source data, and the paper
reproduction package), see [**Citing EuroFlood**](https://cisgroup.github.io/euroflood/citation/).

Developed at Princeton University (Complex Infrastructure Systems Group).

<!-- public-mirror -->
---

_This is the **public mirror** of EuroFlood. Development happens in a separate private
repository; each release here is a clean snapshot. Issues and pull requests are welcome —
PRs are triaged and applied upstream._
