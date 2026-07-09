# CLI & output

The `euroflood` command line mirrors the Python API — every query is also a
command. See **[Tutorial 7 — CLI & configuration](../tutorials/07_cli_and_config.ipynb)**
for a guided tour; this page is the reference for the output contract, global
options, and exit codes, followed by the auto-generated command listing.

## Two output streams

EuroFlood keeps **stdout and stderr cleanly separated** so results are easy to read
interactively and easy to parse on a server:

- **stdout — user-facing results.** Query tables, status confirmations, and progress
  bars are rendered by a [rich](https://rich.readthedocs.io) console. On a non-TTY (a
  pipe, a file, an HPC job) styling and live progress are dropped automatically, so
  redirected output stays plain.
- **stderr — logs.** Structured `structlog` events. On a TTY they render as colored
  `key=value` lines; when piped or run on HPC they render as **one JSON object per
  line**. Importing `euroflood` configures no logging at all — the CLI opts in, and
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

Domain errors are rendered as a **single clean line** with an actionable next step —
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
| `4` | Geocoding failure (place not found) |
| `5` | Network / scraping failure |
| `6` | Hazard tile error |
| `7` | Cache schema mismatch (rebuild or re-mirror the index) |
| `8` | Raster processing error |

Unexpected (non-EuroFlood) exceptions are never swallowed — they surface with a full
traceback so real bugs stay loud.

## Command listing

::: euroflood.cli
