# Get started

Install EuroFlood, run your first flood query, and download a depth map — end to end,
against the **published index** (no data build required). Prefer runnable notebooks? The
**[Tutorials](tutorials/README.md)** cover all of this in depth.

## Install

=== "pip"

    ```bash
    pip install "euroflood[viz]"   # core + plotting (recommended)
    ```

=== "uv"

    ```bash
    uv add "euroflood[viz]"
    ```

EuroFlood works out of the box: the published index is read remotely and cached on first
use (~14 MB of tables), so there is nothing to download or configure first.

## Your first query

`floods(...)` returns a **`FloodFrame`** — a `geopandas.GeoDataFrame`, one row per historic
flood event. It's cheap: it streams a small window of the index and downloads no rasters.

```python
import euroflood as ef

cat = ef.floods("Zutphen, Netherlands")
cat
```

```text
EuroFlood catalogue: 25 flood events · 2015-01-12 … 2024-02-05 · 51.6 km² total
```

Because it *is* a GeoDataFrame, filter and plot it as usual — then `.download()` the depth
rasters for the events you keep and `.stats()` them (no network after the download):

```python
recent = cat[cat["date"] >= "2021-01-01"]    # any pandas / geopandas operation
recent.plot()                                 # recurrence heatmap — still no download
dl = recent.download("out/")                  # fetch + crop only these events' rasters
dl.stats()                                    # max/mean/p95 depth (m), area (km²), volume
```

## Ways to specify a region

A place name is geocoded; a `bbox`, `point` + `radius_m`, or `shapefile` skip geocoding.
Administrative boundaries can be awkward (they often follow a river), so `shape="bbox"` /
`"hull"` gives a cleaner ROI, and `buffer_m` grows it:

```python
ef.floods("Zutphen, Netherlands", buffer_m=1000)
ef.floods("Zutphen, Netherlands", shape="bbox")   # a clean bounding-box ROI
ef.floods(bbox=(6.14, 52.09, 6.27, 52.17))
ef.floods(point=(52.14, 6.20), radius_m=6000)     # point is (lat, lon)
```

## Modelled hazard

The same API queries the global **CEMS-GLOFAS** flood-hazard maps by return period:

```python
haz = ef.hazard("Zutphen, Netherlands", return_period=[100, 500]).download("hazard/")
haz.stats()
```

## Offline & HPC

Everything above streams the index. For fully offline / cluster use, mirror the full
**~130 MB** bundle once, then read it locally:

```bash
export EUROFLOOD_GEOCODER_BACKEND=local   # resolve names from the offline NUTS dataset
euroflood mirror-index                    # pull the whole bundle, then query with no network
```

## Next steps

<div class="grid cards" markdown>

-   :material-school:{ .lg .middle } __Tutorials__

    ---

    The guided path — from a first query to hazard maps and quantitative analysis.

    [:octicons-arrow-right-24: Start the tutorials](tutorials/README.md)

-   :material-lightbulb-on:{ .lg .middle } __Concepts__

    ---

    How the index works — the one page that makes everything else click.

    [:octicons-arrow-right-24: Read Concepts](concepts.md)

-   :material-api:{ .lg .middle } __API reference__

    ---

    Every public function, class, and `EUROFLOOD_*` setting.

    [:octicons-arrow-right-24: Browse the API](reference/index.md)

</div>
