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
# # 4. Download & measure the depth rasters
#
# Everything so far was free (index only). When you want the actual water-depth GeoTIFFs,
# `.download()` fetches and crops **only** the events you selected, and returns the catalogue
# itself, now carrying its files, so the result stays *actionable*. When you run this
# interactively you'll see a **download progress bar**.
#
# > This tutorial downloads real rasters, so it needs network access (it does not run in the
# > offline test suite).

# %%
from pathlib import Path

import euroflood as ef

ef.settings.output_dir = Path("out")  # write crops here (relative, tidy)

cat = ef.floods("Zutphen, Netherlands", shape="bbox")  # bbox ROI (tutorial 02)

# %% [markdown]
# Download a couple of events. Downloads are **cached**: re-running reuses the existing crops
# (nothing is re-fetched) unless you pass `force=True`.

# %%
dl = cat.head(2).download()
dl  # repr shows "… · ⬇ 2/2 downloaded"

# %%
dl.files

# %% [markdown]
# ## Render a downloaded depth map
#
# `.plot(depth=True)` renders the downloaded rasters. When a selection spans several events it
# shows their per-pixel maximum; a guardrail caps auto-downloads at 4 rasters unless you pass
# `limit=` or filter first.

# %%
dl.plot(depth=True)

# %% [markdown]
# ## Work with a single raster: `DepthRaster`
#
# `.depths()` returns lightweight `DepthRaster` objects (also `ef.open_depth(path)`). Each can
# `read()` its array, `plot()` / `explore()` itself, and `save()` to a `.png` or `.html`:

# %%
depth = dl.depths()[0]
array, _transform, crs, _nodata = depth.read()
print(
    "shape:",
    array.shape,
    "| CRS:",
    crs,
    "| max (m):",
    round(float(array.max()) * 0.01, 2),
)

# %%
depth.explore(tiles="grayscale")  # a single event's depth as an interactive overlay

# %% [markdown]
# ```python
# depth.save("zutphen_depth.png")    # static image (by extension)
# depth.save("zutphen_depth.html")   # interactive map
# ```

# %% [markdown]
# ## Measure them: no network, reads the cached rasters
#
# `.stats()` is a per-event table (max / mean / p95 depth in metres, flooded area in km², water
# volume); `.summary()` is the aggregate envelope across all downloaded events. EFAS depth is in
# centimetres and is scaled to metres automatically (pass `scale=1.0` to keep raw units).

# %%
dl.stats()

# %%
dl.summary()

# %% [markdown]
# ## Downloads persist across sessions
#
# A later query of the same area (to the same output dir) automatically finds the crops already
# on disk and pre-fills its `path` column, so you don't need to `.download()` again:

# %%
again = ef.floods("Zutphen, Netherlands", shape="bbox")  # same ROI -> finds the crops
f"{len(again.files)}/{len(again)} already downloaded"

# %%
