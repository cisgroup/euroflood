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
# # 5. Global flood hazard (GLOFAS)
#
# Alongside the *historic* flood maps, `hazard(...)` queries the modelled **CEMS-GLOFAS**
# flood-hazard maps by **return period** (the depth expected on average once every N years).
# Hazard catalogues are just as *actionable* as historic ones — the same `.download()`,
# `.stats()`, `.plot()`, and `.explore()` all work.
#
# > This tutorial downloads real hazard tiles, so it needs network access.

# %%
from pathlib import Path

import euroflood as ef

ef.settings.output_dir = Path("out")

# %% [markdown]
# Query the 100- and 500-year return periods for Zutphen. One row per return period (supported:
# 10, 20, 50, 75, 100, 200, 500):

# %%
haz = ef.hazard("Zutphen, Netherlands", return_period=[100, 500], shape="bbox")
haz

# %% [markdown]
# Download and measure them. GLOFAS depth is already in **metres** and is reported as-is (EFAS
# historic depth is centimetres; the scale is chosen automatically per catalogue).

# %%
haz = haz.download()
haz.stats()

# %% [markdown]
# ## Static and interactive depth maps
#
# Render the 100-year hazard depth as a static image…

# %%
rp100 = haz[haz["return_period"] == 100]
rp100.plot(depth=True)

# %% [markdown]
# …and as an interactive folium overlay you can pan and zoom:

# %%
rp100.explore(depth=True, tiles="grayscale")

# %% [markdown]
# The recurrence/footprint views are historic-only (hazard has no event index), so a hazard
# `.explore()` without `depth=` shows a context map of the queried area:

# %%
haz.explore(tiles="grayscale")

# %%
