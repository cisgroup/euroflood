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
# # 3. Visualize (no download)
#
# With the `viz` extra (`pip install "euroflood[viz]"`), a FloodFrame renders straight from the
# index — no raster download. Static plots use **matplotlib**; interactive maps use **folium**.
# This tutorial tours the two main views (recurrence and footprints) and the options that make
# them presentation-ready.
#
# From here on we query with `shape="bbox"` (introduced in tutorial 02) — a clean rectangular ROI,
# so the maps aren't clipped by Zutphen's jagged boundary that follows the IJssel.

# %%
import euroflood as ef

cat = ef.floods("Zutphen, Netherlands", shape="bbox")

# %% [markdown]
# ## Flood-recurrence heatmap
#
# A bare `.plot()` shows how often each ~90 m pixel flooded (darker = more recorded floods),
# derived from the index alone:

# %%
cat.plot()

# %% [markdown]
# It accepts matplotlib-style options — a different colormap, a fixed `vmax`, and whether to
# draw the query outline:

# %%
cat.plot(cmap="viridis", vmax=5, boundary=False)

# %% [markdown]
# Pass `event_id=` to see a **single event's** footprint instead of the recurrence count:

# %%
one = int(cat.iloc[0]["event_id"])
cat.plot(event_id=one)

# %% [markdown]
# > Add a **basemap** under any static plot with `basemap=True` (needs network for the tiles):
# > `cat.plot(basemap=True, tiles="grayscale")` — presets are `grayscale`, `dark`, `osm`.

# %% [markdown]
# ## Per-event footprints
#
# `.footprints()` returns a GeoDataFrame with one row per event, its `extent_km2`, and (when
# available) `duration_days` — useful as data:

# %%
foot = cat.footprints()
foot[["event_id", "date", "extent_km2", "duration_days"]].head()

# %% [markdown]
# The same footprints as a static map, coloured by year:

# %%
cat.plot(footprints=True)

# %% [markdown]
# ## Interactive maps
#
# `.explore()` returns a folium map. The default **recurrence** view carries hover tooltips
# (how many floods, when) and a click popup listing the dates. Pass `tiles=` for a different
# basemap — `"grayscale"`, `"dark"`, or `"osm"`:

# %%
cat.explore(tiles="grayscale")

# %% [markdown]
# `cat.explore(footprints=True, tiles="grayscale")` instead puts each event on its own
# **toggleable layer**, foldered by year — the interactive twin of the static footprints above.
