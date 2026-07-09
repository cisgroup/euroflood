# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     comment_magics: false
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.4
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # 6. Quantitative analysis
#
# Once the depth rasters are on disk, EuroFlood turns them into numbers — per-event and
# aggregated — so you can rank events, estimate exposure, and compare flooding across years, all
# without leaving Python.
#
# > This tutorial downloads real rasters, so it needs network access.

# %%
from pathlib import Path

import euroflood as ef

ef.settings.output_dir = Path("out")

# Download the depth rasters for the six largest events — enough to compare, and quick.
# (Drop the `.head(6)` to analyse every event; a download progress bar shows while it runs.)
cat = ef.floods("Zutphen, Netherlands", shape="bbox")  # bbox ROI (tutorial 02)
dl = cat.sort_values("area_km2", ascending=False).head(6).download()

# %%
dl

# %% [markdown]
# ## Per-event statistics
#
# `.stats()` returns one row per downloaded event with, for each: the number of wet pixels, the
# **max / mean / p95 depth** (metres), the **flooded area** (km²), and the **water volume**
# (m³ and million m³). EFAS centimetres are scaled to metres automatically.

# %%
stats = dl.stats()
stats

# %% [markdown]
# It is an ordinary DataFrame — rank the events by how much water they moved:

# %%
stats.sort_values("volume_Mm3", ascending=False)[
    ["event_id", "date", "max_depth_m", "flooded_area_km2", "volume_Mm3"]
]

# %% [markdown]
# A quick bar chart of the peak depth per event:

# %%
ax = stats.sort_values("date").plot.bar(
    x="date",
    y="max_depth_m",
    legend=False,
    title="Peak flood depth per event — Zutphen",
)
ax.set_ylabel("max depth (m)")

# %% [markdown]
# ## The aggregate envelope
#
# `.summary()` combines every downloaded raster into a per-pixel **maximum** composite (so
# overlapping floods are counted once) and returns the same measures plus `n_events` — the
# "worst case seen" across the whole record:

# %%
dl.summary()

# %% [markdown]
# ## The depth distribution of one event
#
# Read a single raster and look at where the water actually was — most flooded cells are shallow,
# with a thin tail of deep water:

# %%
import matplotlib.pyplot as plt
import numpy as np

depth = dl.depths()[0]
array, *_ = depth.read()
wet_m = array[array > 0] * 0.01  # centimetres -> metres

_fig, ax = plt.subplots()
ax.hist(wet_m, bins=30)
ax.set(
    xlabel="water depth (m)", ylabel="pixels", title="Depth distribution of one event"
)
print(
    f"{wet_m.size:,} wet pixels · median {np.median(wet_m):.2f} m · max {wet_m.max():.2f} m"
)

# %%
