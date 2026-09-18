# EuroFlood Index — Satellite-Derived Flood Depth Maps for Europe

![Flood-recurrence over Zutphen (river IJssel), built from this index — how often each ~90 m pixel flooded across a decade.](__BASE_URL__/hero.png)

A queryable, cloud-native **spatial index** over the JRC / Copernicus **CEMS-EFAS
Satellite-Derived Flood Depth Maps for Europe** (Betterle & Salamon, 2025). It answers
*which historic floods hit a location, and when* — across Europe, 2015–2025 — **without
downloading the depth rasters**, so a tool can then fetch only the specific flood-depth
GeoTIFFs it actually needs.

- **Coverage:** Europe · 2015–2025 · ~3,610 flood events · ~90 m grid
- **License:** CC-BY-4.0 (see *Attribution* below)
- **Built with:** the open-source [`euroflood`](https://github.com/cisgroup/euroflood) Python library
- **Latest version:** `v__VERSION__/`
__ZENODO_DOI_LINE__

## Layout

Each release is an immutable `vX.Y.Z/` prefix (so a library release always reads the exact
index it was built against):

```
euroflood-index/
  README.md                     ← this file
  v__VERSION__/
    europe_flood_index.tif        sparse Cloud-Optimized GeoTIFF (uint32, EPSG:4326)
    flood_dictionary.parquet      combo_id → flood_ids
    events.parquet                one row per flood event (dates, source URL, …)
    dictionary_meta.json          dictionary metadata
    manifest.json                 schema versions, grid fingerprint, per-file SHA-256
```

## Data model

The index is a **sparse COG** whose pixel values are `combo_id`s — each identifies the
*set* of flood events that inundated that ~90 m cell. `flood_dictionary.parquet` maps
`combo_id → [flood_id, …]`, and `events.parquet` maps `flood_id → {start_date, end_date,
year, source download_url, …}`. So *"which floods hit this area?"* is a spatial mask plus
two joins — **no depth raster download required**.

## Use it

**With the library (recommended):**

```python
import euroflood as ef

cat = ef.floods("Zutphen, Netherlands")  # or a bbox / point+radius / shapefile, + year or period
cat.plot()                         # flood-recurrence heatmap — no download
cat.download().stats()             # fetch depth rasters + max depth / flooded area / volume
```

**Directly (any HTTP / S3 tool — public, no credentials):**

```python
import pandas as pd
base = "__BASE_URL__"
events = pd.read_parquet(f"{base}/events.parquet")
# GDAL / rasterio: /vsicurl/__BASE_URL__/europe_flood_index.tif
```

## Provenance & reproducibility

`manifest.json` pins the grid fingerprint, schema versions, and per-file SHA-256 checksums
for the release. You can rebuild the index yourself from the source maps with `euroflood`.

## Attribution & license

This **derived index** is licensed **CC-BY-4.0**. The underlying flood-depth maps are:

> Betterle, A. & Salamon, P. (2025). *Satellite-Derived Flood Depth Maps for Europe.*
> European Commission, Joint Research Centre (JRC) / Copernicus Emergency Management Service.
> Licensed CC-BY-4.0. <https://data.jrc.ec.europa.eu/dataset/0bc96690-b89c-4909-9166-c2c322a20130>

Please cite **both** the source dataset above and this index.

---

*Maintained via <https://github.com/cisgroup/euroflood>.*
