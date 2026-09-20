# CLI & output

The `euroflood` command line mirrors the Python API: every query is also a
command. See **[Tutorial 7: CLI & configuration](../tutorials/07_cli_and_config.ipynb)**
for a guided tour; this page is the reference for the output contract, global
options, and exit codes, followed by the auto-generated command listing.

## Two output streams

EuroFlood keeps **stdout and stderr cleanly separated** so results are easy to read
interactively and easy to parse on a server:

- **stdout: user-facing results.** Query tables, status confirmations, and progress
  bars are rendered by a [rich](https://rich.readthedocs.io) console. On a non-TTY (a
  pipe, a file, an HPC job) styling and live progress are dropped automatically, so
  redirected output stays plain.
- **stderr: logs.** Structured `structlog` events. On a TTY they render as colored
  `key=value` lines; when piped or run on HPC they render as **one JSON object per
  line**. Importing `euroflood` configures no logging at all. The CLI opts in, and
  library users call `euroflood.setup_logging()` explicitly.

## Global options

These apply to every command and go **before** the sub-command:

| Option | Effect |
|---|---|
| `-v`, `--verbose` | Log at `DEBUG`; show a full traceback on error (for developers). |
| `-q`, `--quiet` | Suppress secondary status and progress; the primary result still prints. |
| `--json` | Emit machine-readable JSON records for `floods`/`hazard` instead of a table. |
| `--no-color` | Disable ANSI styling (`NO_COLOR` is also honored). |

```bash
euroflood floods "Zutphen, Netherlands"        # pretty table + summary line
euroflood --json floods "Zutphen, Netherlands" # JSON records to stdout (pipe to jq)
euroflood -q download "Zutphen, Netherlands" -o out/   # just the result, no chatter
```

## Errors & exit codes

Domain errors are rendered as a **single clean line** with an actionable next step,
never a Python traceback:

```console
$ euroflood floods "Zutphn"
Error [GeocodingError] No local boundary match for 'Zutphn'. Did you mean: zutphen?
→ Try a more specific name, or set EUROFLOOD_ALLOW_REMOTE_GEOCODING=1 for online lookup.
```

Pass `-v` to restore the traceback. Each error class maps to a distinct **exit code**
so scripts can branch on the failure kind:

| Exit | Meaning |
|---|---|
| `0` | Success |
| `1` | Generic EuroFlood error |
| `2` | Missing index / dictionary (build or mirror it first) |
| `3` | Configuration error |
| `4` | Geocoding / region failure (place not found, unknown NUTS id, bad or mismatched CRS) |
| `5` | Network / scraping failure |
| `6` | Hazard tile error |
| `7` | Cache schema mismatch (rebuild or re-mirror the index) |
| `8` | Raster processing error |
| `9` | Publishing failure (Source Cooperative / Zenodo) |
| `10` | Verification failure (published index or local mirror not ready) |

Unexpected (non-EuroFlood) exceptions are never swallowed: they surface with a full
traceback so real bugs stay loud.

## Region options

`floods`, `download`, `hazard` and the `mirror` / `verify` subcommands all select a region the
same way: a `PLACE` argument, or exactly one of `--nuts`, `--bbox`, `--point` and `--shapefile`.

| Option | Meaning |
|---|---|
| `PLACE` | A place name, geocoded (OpenStreetMap, then the offline NUTS dataset). |
| `--bbox MINX MINY MAXX MAXY` | A bounding box, in WGS 84 lon/lat or in `--crs`. |
| `--point LAT LON` | A point, with `--radius` around it. `X Y` (easting, northing) with a projected `--crs`. |
| `--radius M` | Radius in ground metres around `--point`. |
| `--shapefile PATH` | Any vector file. Its own CRS is used; `--crs` fills in a missing one. |
| `--nuts ID` | A Eurostat NUTS region (`NL22`); repeat the option to union several. Find ids with `euroflood nuts`. |
| `--buffer M` | Extra buffer around the region, in ground metres. |
| `--shape exact\|bbox\|hull` | The raw boundary (default), its bounding box, or its convex hull. |
| `--crs CRS` | Coordinate reference system of `--bbox` / `--point`: anything pyproj accepts (`EPSG:28992`, `3035`, WKT). Default: WGS 84 lon/lat. |

```bash
euroflood floods --bbox 200000 455000 220000 475000 --crs EPSG:28992          # Dutch RD New, metres
euroflood hazard --point 308400 5780300 --radius 5000 --crs EPSG:32632 -r 100  # UTM 32N: X Y
euroflood mirror all --bbox 6.1 52.0 6.3 52.2 -r 100                            # WGS 84 (the default)
euroflood nuts Gelderland                       # find NUTS identifiers by name -> NL22, NL224
euroflood nuts --country NL --level 2           # list a country's NUTS-2 regions
euroflood floods --nuts NL22                    # every flood in Gelderland (exact NUTS boundary)
euroflood hazard --nuts NL22 --nuts NL21 -r 100 # the union of two regions
```

A bad or mismatched `--crs`, degrees typed into a metre CRS, or coordinates outside the CRS's
domain exit with code 4 and a one-line hint naming the fix.

## Command listing

::: euroflood.cli
