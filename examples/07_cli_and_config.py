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
# # 7. The command line & configuration
#
# Everything you can do in Python has a CLI equivalent, and every behaviour is configurable via
# `ef.settings` (or an `EUROFLOOD_*` environment variable). This is a reference for both.

# %% [markdown]
# ## The `euroflood` CLI
#
# ```bash
# euroflood floods "Zutphen, Netherlands"                        # print a catalogue
# euroflood floods "Zutphen, Netherlands" --start 2024 -o zutphen.geojson  # filter + write (CSV/GeoJSON/Parquet)
# euroflood floods "Zutphen, Netherlands" --query "area_km2 > 1"  # a pandas .query() expression
# euroflood floods "Zutphen, Netherlands" --shape bbox           # a clean bounding-box ROI
# euroflood download "Zutphen, Netherlands" --start 2024 --out out/  # search + download + crop
# euroflood hazard "Zutphen, Netherlands" -r 100 -r 500 --download --out hazard/
# euroflood --json floods "Zutphen, Netherlands" | jq '.[0]'     # machine-readable JSON
# euroflood floods "Zutphen, Netherlands" --dry-run              # show the plan, change nothing
# ```
#
# Global flags: `-v/--verbose`, `-q/--quiet`, `--json`, `--no-color`, `--version`.

# %% [markdown]
# ## Configuration in Python
#
# `ef.settings` is a Pydantic model: inspect it, or read/write individual fields. Every field
# also has an `EUROFLOOD_<NAME>` environment variable.

# %%
import euroflood as ef

print("index_mode      :", ef.settings.index_mode)
print("geocoder_backend:", ef.settings.geocoder_backend)
print("show_progress   :", ef.settings.show_progress)
print("geocode_cache   :", ef.settings.geocode_cache)
print("downloads go to :", ef.settings.output_dir.name + "/  (settings.output_dir)")

# %% [markdown]
# Override at runtime (equivalent to setting the env var before launch):

# %%
from pathlib import Path

ef.settings.output_dir = Path("results")  # or: export EUROFLOOD_OUTPUT_DIR=results
ef.settings.show_progress = True  # download/mirror bars when interactive
ef.settings.output_dir

# %% [markdown]
# ## Fast & offline
#
# By default EuroFlood streams the published index over the network and geocodes place names
# online (both cached after first use). For fully offline / HPC / reproducible runs:
#
# ```bash
# export EUROFLOOD_GEOCODER_BACKEND=local   # resolve names from the offline NUTS dataset
# export EUROFLOOD_OFFLINE=1                 # both collections cache-only, geocoder offline
# euroflood mirror all --bbox 6.1 52 6.3 52.2 -r 100   # stage a region for offline use
# euroflood floods --bbox 200000 455000 220000 475000 --crs EPSG:28992  # a region in another CRS
# euroflood nuts Gelderland                  # find a Eurostat NUTS identifier -> NL22
# euroflood floods --nuts NL22               # every flood in that NUTS region
# ```
#
# …or pass a `bbox=` / `point=` to skip geocoding, and `EUROFLOOD_SHOW_PROGRESS=0` to silence
# progress bars (e.g. in CI).

# %% [markdown]
# All of this is just configuration around the same query you already know:

# %%
ef.floods("Zutphen, Netherlands").head()
