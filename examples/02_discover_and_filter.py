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
# # 2. Discover & filter flood events
#
# `floods()` is the entry point for *discovery*. It resolves your area of interest, reads a
# small window of the published index, and returns a **FloodFrame** — a
# `geopandas.GeoDataFrame` with one row per historic flood event. It is cheap (a few MB, no
# depth rasters), so you explore freely and only download later.
#
# This tutorial covers the ways to specify a region, how to filter by time, and how to work
# with the result as an ordinary GeoDataFrame.

# %%
import euroflood as ef

# %% [markdown]
# ## Ways to specify a region
#
# A **place name** is geocoded (OpenStreetMap, cached after the first lookup); a **bbox**,
# **point + radius**, or **shapefile** skip geocoding entirely. All return a FloodFrame for the
# same kind of query:

# %%
cat = ef.floods("Zutphen, Netherlands")  # place name (geocoded)
cat

# %%
ef.floods(bbox=(6.15, 52.10, 6.26, 52.17))  # bounding box (minx,miny,maxx,maxy) WGS84

# %%
ef.floods(point=(52.14, 6.20), radius_m=6000)  # a point (lat, lon) + a radius in metres

# %% [markdown]
# ### Cleaner ROIs with `shape=`
#
# Administrative boundaries are *legal* shapes, not hydrological ones — Zutphen's gemeente
# boundary runs right down the IJssel, so the raw outline is jagged and cuts the river out.
# Pass `shape="bbox"` (a bounding rectangle) or `shape="hull"` (the convex hull) to query a
# clean ROI instead of the raw boundary; `buffer_m` still applies on top.

# %%
raw = ef.floods("Zutphen, Netherlands")  # raw admin boundary (follows the river)
box = ef.floods("Zutphen, Netherlands", shape="bbox")  # a clean enclosing rectangle
print(f"raw boundary: {len(raw)} events   ·   shape='bbox': {len(box)} events")
box.plot()

# %% [markdown]
# A few more, shown for reference:
#
# ```python
# ef.floods("Zutphen, Netherlands", buffer_m=1000)  # a place plus a 1 km buffer
# ef.floods(shapefile="my_area.geojson")            # any vector file as the ROI
# ef.floods("Gelderland", level=2)                  # disambiguate by NUTS level (0=country … 3=province)
# ```

# %% [markdown]
# ## Filter by time
#
# Keep a single `year`, or an open/closed date range with `start` / `end` (`"YYYY"` or
# `"YYYY-MM-DD"`). The IJssel floods most winters, so even a single year usually holds a few
# events:

# %%
ef.floods("Zutphen, Netherlands", start="2024-01-01", end="2024-12-31")

# %% [markdown]
# ## It is a GeoDataFrame
#
# The result is a real GeoDataFrame, so the whole pandas / geopandas API is available — no new
# query language to learn:

# %%
cat["year"].value_counts().sort_index()  # how many events per year

# %%
cat.query("area_km2 > 1").sort_values("date")[["event_id", "date", "year", "area_km2"]]

# %% [markdown]
# ## Export the catalogue
#
# Write it to CSV (drop the geometry for a flat table), GeoJSON, or GeoParquet:

# %%
cat.drop(columns="geometry").to_csv("zutphen.csv", index=False)
cat.to_file("zutphen.geojson", driver="GeoJSON")
cat.to_parquet("zutphen.parquet")

# %% [markdown]
# ## A note on speed & caching
#
# The first `floods()` in a session mirrors the ~14 MB dictionary + events tables once (cached
# across sessions), geocodes the place name (**cached on disk** after the first lookup), and
# streams the index window over the network. Repeat queries are fast. For *fully offline* or
# HPC use:
#
# ```bash
# export EUROFLOOD_GEOCODER_BACKEND=local   # resolve names from the offline NUTS dataset
# euroflood mirror-index                    # pull the whole index once, then read it locally
# ```
#
# …or pass a `bbox=` / `point=` to skip geocoding altogether.
