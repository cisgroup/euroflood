# Get started

Install EuroFlood, run your first flood query, and download a depth map, end to end,
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
use (~19 MB of tables), so there is nothing to download or configure first.

## Your first query

`floods(...)` returns a **`FloodFrame`**: a `geopandas.GeoDataFrame`, one row per historic
flood event. It's cheap: it streams a small window of the index and downloads no rasters.
That's the **discover** step of EuroFlood's discover → extract model; you **extract** depth
rasters only for the events you keep.

```python
import euroflood as ef

cat = ef.floods("Zutphen, Netherlands")
cat
```

```text
EuroFlood catalogue: 26 flood events · 2015-01-12 … 2024-12-30 · 52.8 km² total
```

Because it *is* a GeoDataFrame, filter and plot it as usual, then `.download()` the depth
rasters for the events you keep and `.stats()` them (no network after the download):

```python
recent = cat[cat["date"] >= "2021-01-01"]    # any pandas / geopandas operation
recent.plot()                                 # recurrence heatmap, still no download
dl = recent.download("out/")                  # fetch + crop only these events' rasters
dl.stats()                                    # max/mean/p95 depth (m), area (km²), volume
```

## Ways to specify a region

A place name is geocoded; a `bbox`, `point` + `radius_m`, or `shapefile` skip geocoding.
Administrative boundaries can be awkward (they often follow a river), so `shape="bbox"` /
`"hull"` gives a cleaner ROI, and `buffer_m` grows it (in ground metres):

```python
ef.floods("Zutphen, Netherlands", buffer_m=1000)
ef.floods("Zutphen, Netherlands", shape="bbox")   # a clean bounding-box ROI
ef.floods(bbox=(6.14, 52.09, 6.27, 52.17))        # (minx, miny, maxx, maxy) in WGS 84
ef.floods(point=(52.14, 6.20), radius_m=6000)     # point is (lat, lon) in WGS 84
```

Working with Eurostat statistics? Select a **NUTS region** by its identifier and get exactly
that administrative boundary (from the Eurostat GISCO 1:1M files): `NL` is the country, `NL2` a
major region, `NL22` Gelderland, `NL225` a small region; the identifier's length gives the
level. `ef.nuts()` finds identifiers by name or lists a country's regions:

```python
ef.nuts("Gelderland")                                # -> NL22 (level 2), NL224 Zuidwest-Gelderland
ef.floods(nuts="NL22")                               # every flood in Gelderland
ef.hazard(nuts=["NL22", "NL21"], return_period=100)  # the union of two regions
ef.nuts(country="NL", level=2)                       # all Dutch NUTS-2 regions (loop for per-region catalogues)
```

Coordinates default to WGS 84 lon/lat. Pass `crs=` (anything pyproj accepts: an EPSG code, an
authority string, WKT) to give them in another system. A `bbox` is always
`(minx, miny, maxx, maxy)`; a `point` is `(x, y)` = (easting, northing) in a projected CRS. A
shapefile keeps its own CRS (`crs=` only fills in a missing one). The catalogue you get back is
always EPSG:4326: call `.to_crs()` on it for anything else.

```python
ef.floods(bbox=(200000, 455000, 220000, 475000), crs="EPSG:28992")   # Dutch RD New, metres
ef.floods(point=(308400, 5780300), radius_m=6000, crs="EPSG:32632")  # UTM 32N: point is (x, y)
```

## Modelled hazard

The same API queries the global **CEMS-GLOFAS** flood-hazard maps by return period
(seven are available: 10, 20, 50, 75, 100, 200, and 500 years):

```python
haz = ef.hazard("Zutphen, Netherlands", return_period=[100, 500]).download("hazard/")
haz.stats()
```

## Offline & HPC

Everything above streams data on demand. For a fully offline / cluster node, **mirror
the layers you need once** (on a machine with internet), then flip the node offline:

```bash
# On a networked login node, stage a study region for offline use:
euroflood mirror all --bbox 6.1 52.0 6.3 52.2 -r 100   # index + flood depths + hazard tiles
euroflood verify all --bbox 6.1 52.0 6.3 52.2 -r 100 --deep   # readiness gate (checksums)

# On the offline compute node, one switch forces everything cache-only:
export EUROFLOOD_OFFLINE=1
euroflood download --bbox 6.1 52.0 6.3 52.2 --out out/
euroflood hazard --bbox 6.1 52.0 6.3 52.2 -r 100 --download --out out/
```

`mirror` stages any layer independently: `mirror index` (the flood catalogue, so
`floods()` **queries** run offline), `mirror floods --bbox …` (the flood **depth maps**
for a region), `mirror hazard --bbox …` (GLOFAS **hazard tiles** for a region), or
`mirror all` for everything. `verify` reports what is present / missing / corrupt (`--deep`
re-checks sha256). `EUROFLOOD_OFFLINE=1` (or `euroflood.offline()` in Python) forces both
collections cache-only and the geocoder to the offline NUTS backend; a missing tile then
raises a clear error naming the exact `mirror` command to run, never a silent partial
result. In Python: `ef.mirror("hazard", bbox=(6.1, 52.0, 6.3, 52.2), return_period=100)`.

## Next steps

<div class="grid cards" markdown>

-   :material-school:{ .lg .middle } __Tutorials__

    ---

    The guided path: from a first query to hazard maps and quantitative analysis.

    [:octicons-arrow-right-24: Start the tutorials](tutorials/README.md)

-   :material-map-search:{ .lg .middle } __Case studies__

    ---

    Real-world flood analyses on genuine events across Europe, from Storm Boris to
    the Valencia DANA.

    [:octicons-arrow-right-24: Browse case studies](case-studies/README.md)

-   :material-lightbulb-on:{ .lg .middle } __Concepts__

    ---

    How the index works: the one page that makes everything else click.

    [:octicons-arrow-right-24: Read Concepts](concepts.md)

-   :material-api:{ .lg .middle } __API reference__

    ---

    Every public function, class, and `EUROFLOOD_*` setting.

    [:octicons-arrow-right-24: Browse the API](reference/index.md)

</div>
