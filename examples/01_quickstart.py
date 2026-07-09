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
# # 1. Quickstart
#
# Discover historic floods anywhere in Europe in a few lines, straight from the published
# index. There is nothing to download or configure first.
#
# Install with plotting support:
#
# ```bash
# pip install "euroflood[viz]"
# ```

# %%
import euroflood as ef

# %% [markdown]
# `floods(...)` queries the index for a place and returns a **FloodFrame** - a
# `geopandas.GeoDataFrame` with one row per historic flood event. It is cheap: it streams a
# small window of the index and downloads **no** flood-depth rasters.
#
# We follow one place through these tutorials: **Zutphen**, a Hanseatic town on the river
# IJssel in the Netherlands that floods regularly - so its catalogue is rich enough to be
# interesting.

# %%
cat = ef.floods("Zutphen, Netherlands")
cat.head()

# %% [markdown]
# A bare `.plot()` renders a flood-recurrence heatmap - how often each ~90 m pixel flooded -
# straight from the index (still nothing downloaded):

# %%
cat.plot()

# %% [markdown]
# That is the whole idea: find *where and when* floods happened for free, then fetch only the
# depth rasters you actually want. From here:
#
# - **02 - Discover & filter**: query by bbox / point / shapefile and filter by time.
# - **03 - Visualize**: per-event footprints and interactive maps.
# - **04 - Download & measure**: fetch the depth rasters and compute statistics.
# - **05 - Hazard**: modelled GLOFAS flood-hazard maps by return period.
# - **06 - Quantitative analysis**: rank events, estimate exposure, compare depths.
# - **07 - CLI & configuration**: the `euroflood` command line and every setting.
