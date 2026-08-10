# Changelog

All notable changes to EuroFlood are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/) once it reaches a public release.

## [Unreleased]

## [0.2.2] - 2026-08-10

### Fixed
- `floods()` discovery (and therefore `mirror floods`) no longer materializes the
  whole ROI window of the index raster in memory. Very large ROIs (continental
  bounding boxes, "mirror everything") previously allocated tens of GB and were
  OOM-killed with a bare `exit 137`; the index is now read in block-aligned chunks
  with the ROI mask applied per chunk, so memory stays flat (~64 MB working set)
  for any ROI, with identical query results. A full-archive query
  (`bbox=(-180, -90, 180, 90)`) now completes on an ordinary machine. (#34)

## [0.2.1] - 2026-07-23

### Added
- **Case studies** in the docs: six real-world flood analyses reproduced from the
  EuroFlood paper (Storm Boris, a decade at Zutphen, the Shannon callows, the Valencia
  DANA, the Baltic surge, and a continental decade of recurrence), rendered from committed
  notebook outputs. They ship to the docs site and GitHub but are excluded from the PyPI
  distribution to keep it lean.
- A **"Citing EuroFlood"** docs page collecting the software, index-dataset, and
  source-data citations.

### Changed
- Documentation enriched from the paper: the landing page gains a continental
  flood-recurrence showcase; **Concepts** gains the index schematic, a compactness
  explanation, and new "How complete, and how trustworthy" and "Interpreting your
  results" sections (validation and limitations). The two-stage access model is now
  consistently called **Discover → Extract**, and figures/numbers are aligned with the
  paper (e.g. ~18.9 GB source archive, ~138 MB index, seven GLOFAS return periods).
- Auto-generated plot titles now use a colon instead of an em-dash (e.g. "Flood depth:
  max of 2 events"), and the documentation (docs, tutorials, case studies, docstrings,
  and README) avoids em-dashes throughout.

## [0.2.0] - 2026-07-13

### Added
- **Unified offline / HPC mirror family**: a `mirror` command group stages any data layer into
  the cache for offline use: `mirror index` (the flood catalogue, so `floods()` queries run
  offline), `mirror floods --bbox …` (historic flood **depth maps** for a region), `mirror hazard
  --bbox …` (GLOFAS **hazard tiles** for a region), and `mirror all`. A matching `verify` group
  (`verify index|floods|hazard|all|remote`, `--deep`) reports local-mirror readiness
  (present/missing/corrupt) against a sha256+size ledger. Region-scoped mirrors keep the HPC
  footprint small and accumulate across runs. Python: `euroflood.mirror(target, …)` /
  `euroflood.verify(target, …)`.
- **`EUROFLOOD_OFFLINE` / `euroflood.offline()`**: one switch forces both collections cache-only
  (no network) and the geocoder to the local NUTS backend. A new `hazard_mode` setting
  (`auto`/`local`/`remote`) mirrors `index_mode`; in offline mode a missing tile/index raises a
  clear error naming the exact `mirror` command instead of failing silently.

### Changed
- **CLI mirror/verify commands regrouped (breaking).** The consumer offline commands are now
  subcommands: `mirror-index` → `mirror index`, `mirror-hazard` → `mirror hazard`, `verify-remote`
  → `verify remote`. The producer bulk-download command `mirror` (the ~35 GB raw-tile fetch used to
  *build* the index) is renamed `fetch-sources` to free the `mirror` namespace for the consumer
  group. The Python `euroflood.mirror_hazard()` is superseded by `euroflood.mirror("hazard", …)`.
  (Pre-1.0: no backward-compatible aliases are kept.)

### Fixed
- **Silent hazard truncation on a partial GLOFAS tile fetch.**
  For an ROI spanning more than one GLOFAS tile, `hazard(...).download()` dropped any tile whose
  fetch failed and mosaicked whatever subset survived, warning only when the set was *completely*
  empty. A single flaky JRC tile fetch therefore produced a valid-looking GeoTIFF covering only
  part of the ROI, with no error, silently corrupting any multi-return-period sweep. It now **fails
  closed**: an incomplete tile set is never mosaicked. A return period whose tiles cannot all be
  fetched is skipped with a warning (its raster is simply absent, not truncated), the complete
  sibling return periods still succeed, and a wholly-failed request raises. Crops are also written
  atomically, and each hazard raster now carries `EUROFLOOD_SOURCE_TILES` / `N_SOURCE_TILES` /
  `RETURN_PERIOD` GeoTIFF tags so its tile coverage is auditable.

## [0.1.0] - 2026-07-09

### Added
- **ROI `shape` option**: `floods()`/`hazard()` (and the `floods`/`download`/`hazard` CLI commands via
  `--shape`) take `shape="exact"` (default, the raw admin boundary), `"bbox"` (its bounding rectangle),
  or `"hull"` (its convex hull). Administrative boundaries are legal, not hydrological, shapes: they
  often run down a river (excluding it), are oddly shaped, or split into exclaves; a bbox/hull ROI
  sidesteps that (and collapses a MultiPolygon into one clean shape). Applies to every region input,
  turns `point`+`radius_m` into a bounding square, and composes with `buffer_m` (which then grows the
  chosen shape). Default `"exact"` keeps existing behaviour unchanged.
- **Tutorials** (`examples/`): a top-level folder of runnable, progressive Jupyter tutorials
  (Quickstart → Discover & filter → Visualize → Download & measure → Hazard → Quantitative
  analysis → CLI & configuration), jupytext-paired (`.py` + `.ipynb`), rendered on the docs
  site (mkdocs-jupyter) and executed as tests: the cheap-path ones offline against a committed
  fixture on every PR, all of them live monthly (and on demand). Supersedes the old root `demo.py`/`demo.ipynb`.
- **Notebook/TTY progress bars**: `.download()` shows a transfer bar and the first-query index
  mirror shows activity when running interactively (a Jupyter notebook or a TTY). Toggle with
  `settings.show_progress` / `EUROFLOOD_SHOW_PROGRESS`.
- **Persistent geocode cache**: resolved place-name geometries are cached on disk
  (`cache_dir/geocode/`), so a repeat `floods("Cologne")` skips the Nominatim round-trip. Toggle
  with `settings.geocode_cache` / `EUROFLOOD_GEOCODE_CACHE`.
- **Quiet by default**: `import euroflood` no longer leaks structlog / pooch INFO lines to
  stdout; the library is silent unless you call `ef.setup_logging(...)`.
- **Faster repeat queries**: the index COG is opened once per process and reused (with GDAL's
  in-process block cache), so a repeat `floods()` (and the `.plot()` / `.explore()` /
  `.footprints()` recurrence views, which also read the index) are near-instant (~0.03–0.07s)
  instead of re-opening the remote COG every call.
- **Publishing** (`euroflood publish`): one-command upload of the built index bundle to
  **Source Cooperative** (the live `/vsicurl` host, via the optional `euroflood[publish]`
  extra) and/or **Zenodo** (citable DOI). The manifest is stamped with the version + public
  URLs, and the hosted index is self-verified after upload. A reusable `verify_published()`
  helper, a `euroflood verify-remote` CLI, and an opt-in `online` test suite check the live
  index end-to-end (manifest, `/vsicurl` COG, a real query) without touching offline CI.
- **Zero-config remote index**: `DEFAULT_INDEX_BASE_URL` is baked to the published Source
  Cooperative prefix, so a plain `pip install euroflood` reads the index with no setup.
- `euroflood.__version__` and `euroflood --version`.
- `RELEASING.md`: the two-track (library → PyPI, index → Source Cooperative / Zenodo)
  release procedure; `release.yml` now fails a release when the git tag ≠ the package version.
- **Visualization** (optional `euroflood[viz]` extra): a flood-recurrence heatmap
  as the default `FloodFrame.plot()` / `.explore()`; per-event flood extents via
  `.footprints()` (a GeoDataFrame with `extent_km2`); an opt-in downloaded
  depth-map view (`depth=True`, `plot_depth`, `open_depth`, `DepthRaster`); and
  interactive folium maps with hover tooltips. Static via matplotlib, interactive
  via folium.
- **CLI feedback layer**: a unified `rich` console for result tables, status, and
  progress; global options `-v/--verbose`, `-q/--quiet`, `--json`, `--no-color`;
  clean one-line error messages with per-error-type exit codes (no tracebacks);
  a notebook-friendly `FloodFrame` summary repr.
- **Documentation**: a Concepts page explaining the index data model; a real
  consumer Getting Started walkthrough; full API reference coverage for the
  consumer API (`floods`, `hazard`, `download`, `FloodFrame`, configuration);
  `CONTRIBUTING.md`, `CITATION.cff`, and this changelog.


### Changed
- **Leaner `viz` extra.** Removed the unused `mapclassify` dependency from the optional
  `euroflood[viz]` extra, which drops its heavy transitive tail (scikit-learn, scipy, networkx)
  from the install, six fewer packages. `mapclassify` only serves geopandas' choropleth
  *classification* schemes, which EuroFlood never uses (it draws folium via direct `GeoJson`/
  `ImageOverlay`), so `.plot()` / `.explore()` are unchanged.
- **Documentation overhaul.** A cohesive azure "water" theme (custom palette + `extra.css`, logo +
  favicon, light/dark toggle); a real landing page (hero flood map, feature cards, CTAs, badges) that
  replaces the wall of text; **real Zutphen figures** with a grayscale basemap ("flooding in front of a
  map") generated by `scripts/make_doc_images.py`, replacing the placeholder images; a **lean 5-section
  IA** (Home · Get started · Tutorials · Concepts · API reference) that folds the redundant
  Visualization/Cookbook/Output pages into the tutorials/reference and moves the internal Vision &
  Related-datasets notes off the site; a tutorials **gallery** with thumbnails; and the section renamed
  "Reference" → "API reference". Fixes the raw-`.py` tutorial links (the `docs/tutorials → examples`
  symlink no longer leaks `.py`/run artifacts, via `exclude_docs`) and the inconsistent notebook H1s.
  Docstring cross-references were converted from raw reStructuredText roles (`:func:`/`:class:`, which
  rendered literally under the Google docstring style) to plain code spans, so the API reference reads
  cleanly.
- **`plot(basemap=True)` fix.** The contextily basemap was drawn *over* the flood raster (muting the
  overlay); it now draws behind it (`zorder=-1`), so the flood reads clearly against the map.
- **Tutorials & docs now headline Zutphen** (on the IJssel, NL) instead of Cologne/Valencia:
  its recurrent river flooding makes a richer example and motivates `shape="bbox"` (its gemeente
  boundary follows the river). The offline test fixture was regenerated for Zutphen (with an
  appended OSM municipality boundary so the offline geocoder resolves it), tutorial 02 gained an
  executed `shape="bbox"` beat, and a latent `point=(lat, lon)` argument-order slip in the region
  examples was fixed.
- **Parallel downloads**: `.download()` now fetches source rasters concurrently
  (up to `settings.max_workers_dl`, default 8) instead of one at a time, a roughly
  N× faster wall-clock for multi-event historic catalogues and multi-tile hazard
  layers. Output files, paths, and cache-hit skipping are unchanged; a per-file
  step bar replaces the byte bar for the library (notebook/TTY) path, while the CLI
  keeps its byte bar via `on_bytes`.
- **Fewer per-call rebuilds**: the DuckDB connection (dictionary + events lookups)
  and the ~14 MB NUTS boundary dataset (offline `local` geocoder) are now cached
  per process instead of being reopened/reloaded on every query, the same
  "reuse, don't re-init" fix already applied to the index COG. Repeat queries and
  `local`-backend batches are correspondingly faster.
- **Faster hazard stats and streaming reads**: `hazard(...).stats()` / `.summary()`
  read each depth raster once (was twice, to inspect the CRS first), and streaming
  `/vsicurl` hazard reads now run under the same GDAL env as the index COG.
- `import euroflood` no longer eagerly imports the producer pipelines
  (`ExportPipeline`, `IngestionPipeline`) or their scraper/BeautifulSoup stack;
  they resolve lazily on first use, so the consumer import is lighter.
- `Settings` fields are now self-documenting via `Field(description=...)`, visible
  in `help()`, IDE tooltips, and the Configuration reference.
- Replaced `tqdm` with `rich` for progress; `structlog` still owns machine/JSON
  logs on stderr (HPC-friendly), while user-facing output goes to stdout.


### Removed
- Stale `readme.org` (superseded by `README.md`).


### Fixed
- Corrected stale docstrings (geocoder default backend, the `extract` CLI verb,
  the legacy `ExtractionPipeline` dictionary format).
