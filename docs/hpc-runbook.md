# Building the index (producers)

!!! note "Most users never need this"
    The published index is read remotely with zero setup. This page is only for
    **producers** rebuilding the full-scale index from the source archive.

This is the end-to-end recipe for building the real, full-scale historic flood index
on an HPC cluster and producing the **deployable bundle** (a single COG + a
partitioned-Parquet dictionary + events table + a checksummed manifest) that
`euroflood publish` uploads to Source Cooperative (and optionally mints a Zenodo DOI).

The index grid is **131,760 × 69,720 px (~9.18 billion pixels, ~37 GB uint32)** but
**sparse**, so it compresses to a few hundred MB. Ingestion is embarrassingly
parallel; the reduce/build runs on one fat node.

## Princeton Della (the concrete, recommended path)

Della **compute nodes have no internet**, so the work splits cleanly: **download
where there's internet, process where there's compute.** Three ready scripts live
in `scripts/slurm/`. Edit the `/scratch/gpfs/<GROUP>/$USER` path and `--mail-user`
for your group/NetID.

| Step | Where | Script |
|---|---|---|
| 1. Download ~35 GB (mirror) | **della-vis1/2** (internet, no scheduler) | `della_mirror.sh` |
| 2. Process → Parquet (offline) | `cpu` partition (SLURM) | `della_ingest.sbatch` |
| 3. Build the index COG (offline) | `cpu` partition (SLURM) | `della_build_index.sbatch` |

```bash
# 1) on a visualization node (internet), also warms the uv cache + builds .venv
ssh <NetID>@della-vis1.princeton.edu
cd /scratch/gpfs/<GROUP>/$USER/euroflood
tmux new -s mirror && bash scripts/slurm/della_mirror.sh   # tmux survives disconnects

# 2) + 3) from a login node, submit the offline compute jobs
mkdir -p logs
sbatch scripts/slurm/della_ingest.sbatch         # ~6 min on 32 cores (3280 tiles, peak ~14 GB)
# confirm COMPLETED via sacct/seff (NOT the .out), then run the next step on its own:
sbatch scripts/slurm/della_build_index.sbatch    # ~3 min -> COG + dictionary + manifest
```

Why this shape (per the [Princeton RC docs](https://researchcomputing.princeton.edu/systems/della)):

- **No internet on compute nodes** → downloads *and* `uv sync` happen on della-vis/login;
  the compute jobs run `UV_OFFLINE=1` off the cache the mirror warmed. (`uv sync` on a
  compute node would hang.)
- **One `cpu` partition, QOS auto-set by `--time`** → both compute jobs finish in minutes, so
  they set `--time` < 61 min to ride the fast `test` QOS (verified to allow 32c/256 G); ≤ 24 h
  would use `short`. Standard nodes are 192-core / ~1.5 TB.
- **All job I/O on `/scratch/gpfs/<group>/$USER`** (never `/home`, which is ~10–50 GB); the
  uv cache is redirected there too.
- **Thread pinning** → `ingest` pins BLAS/GDAL to one thread (its process pool, sized to
  `$SLURM_CPUS_PER_TASK`, provides the parallelism); `build-index` does the opposite (one
  process, `GDAL_NUM_THREADS=ALL_CPUS`).
- **Bounded memory (why 64 G is enough)** → the tiles are mostly-nodata uint16 masks that
  decompress ~1000x (~10 MB on disk → up to ~20 GB in RAM), so `ingest` reads each raster in
  row stripes and caps the per-process GDAL block cache (`GDAL_CACHEMAX=256`): measured peak
  ~14 GB across 32 workers on a real Della run. Without both, 32 whole-tile reads OOM'd even
  at 256 G.
- rasterio's wheels bundle GDAL ≥ 3.8 → the COG driver works with **no `module load`**.

The generic, scheduler-agnostic recipe follows.

## 0. Prerequisites

- `uv sync` available on the cluster; GDAL ≥ 3.1 (for the COG driver) via the
  project's `rasterio` wheel.
- A **shared** scratch path visible to all nodes, e.g. `/scratch/$USER/euroflood`.
  Everything keys off `EUROFLOOD_CACHE_DIR`.

## 1. Scrape the inventory once

The inventory (`inventory.csv`) must exist before ingest (each shard, if you shard,
selects its slice from it). Any single run scrapes and caches it:

```bash
export EUROFLOOD_CACHE_DIR=/scratch/$USER/euroflood
uv run euroflood ingest --year 2020        # scrapes inventory.csv, ingests 2020
```

## 1b. (Optional but recommended) Download first, then process

For large bulk fetches it's more robust to **download all source tiles first**, then
process from the local cache. The full EFAS archive is **~35 GB across ~3,280 tiles**
(mostly-nodata uint16 masks: ~10 MB compressed but up to ~20 GB uncompressed; `ingest`
streams them in row stripes so RAM stays bounded).

```bash
euroflood fetch-sources            # download-only: all tiles -> cache/downloads (resumable)
euroflood fetch-sources --verify   # re-download any cached file whose size != the JRC listing
euroflood fetch-sources --retry-failed   # re-attempt only the dead-letter list
```

`fetch-sources` (the producer bulk download, formerly `mirror`) is resumable (skips files
already present), ledger-tracked (`mirror_*.jsonl` + a `failed_mirror_*.json` dead-letter),
and download-only. A subsequent `ingest` cache-hits every downloaded file and just processes
it. (For the *consumer* offline mirror, staging a published region for offline use, see
`euroflood mirror` / `verify` below.)

### Offline consumer mirror (compute nodes)

To run `floods()`/`hazard()` on an offline node, stage the layers you need on a networked
node, then flip the compute node offline:

```bash
# networked login node, stage a region (index + flood depths + hazard tiles):
euroflood mirror all --bbox 6.1 52.0 6.3 52.2 -r 100
euroflood verify all --bbox 6.1 52.0 6.3 52.2 -r 100 --deep   # readiness gate

# compute node with no egress:
EUROFLOOD_OFFLINE=1 euroflood hazard --bbox 6.1 52.0 6.3 52.2 -r 100 --download --out out/
```

`mirror index|floods|hazard|all` write a sha256+size ledger; `verify …` (add `--deep` to
re-hash) reports present/missing/corrupt. `EUROFLOOD_OFFLINE=1` forces both collections
cache-only; a missing tile raises a clear error naming the exact `mirror` command to run.
Run the consumer mirror as a single process (the ledger is a read-modify-write JSON); to
parallelise across regions, give each a distinct `EUROFLOOD_CACHE_DIR` and merge afterward.

## 2. Ingest (process tiles → Parquet)

`ingest` is resumable and ledger-tracked: it skips already-processed files
(`process_status` in {complete, empty}) and writes a `failed_*.json` dead-letter for
the rest, so re-submitting safely resumes. On Della the whole archive processes on a
**single node in ~6 min** (a 32-worker process pool), so the single-node job is the
proven path:

```bash
sbatch scripts/slurm/della_ingest.sbatch     # 32 workers; all years, offline cache-hits
```

For clusters where one node isn't enough, the CLI shards (scheduler-agnostic): each
shard is deterministic (`global_id % shard_count == shard_index`) and writes
globally-unique Parquet to the shared lake, so there's **no merge step**. Roll your own
SLURM array around it:

```bash
uv run euroflood ingest --shard-count 64 --shard-index $SLURM_ARRAY_TASK_ID
```

**Node-local scratch (optional):** on a slow shared FS, set
`EUROFLOOD_CACHE_DIR=$TMPDIR/euroflood` for the heavy work, then `cp -rn` the
unique-named Parquet + ledgers back to the shared lake.

## 3. Build the index (one node)

Reduces the Parquet lake into the bundle and validates it. Single-process but
thread-parallel (DuckDB aggregation + the GDAL COG driver); profiled at only ~2.3 cores
used and ~73 GB peak, so it's sized to 4 cores / ~112 GB and runs in **~3 min**:

```bash
sbatch scripts/slurm/della_build_index.sbatch
```

Equivalent CLI:

```bash
export EUROFLOOD_INDEX_MEMORY_LIMIT=96GB   # DuckDB budget < job --mem, so it spills, not OOMs
export EUROFLOOD_INDEX_THREADS=4           # only the brief aggregation is core-hungry
export GDAL_CACHEMAX=4096                   # MB (single process here, so a big cache is fine)
uv run euroflood build-index               # export + doctor
```

`build-index` runs the export (materialize the aggregation **once** → block-write a
sparse tiled BigTIFF → COG-ify with overviews via the GDAL COG driver → write the
Parquet dictionary → author the publish manifest) and then the **doctor** gate.

Produces, under `EUROFLOOD_CACHE_DIR`:

```
europe_flood_index.tif        # the single sparse COG (overviews, nodata=0)
flood_dictionary.parquet/     # partitioned: combo_bucket=N/*.parquet
dictionary_meta.json          # schema/version/bucket layout
manifest.json                 # publish manifest (provenance + checksums + COG attestation)
```

## 4. Validate (the publish gate)

`build-index` already runs it; to re-validate a bundle:

```bash
uv run euroflood doctor
```

The doctor checks the COG is a valid tiled COG with overviews + `nodata=0`, the
grid fingerprint matches the code, the manifest checksums match on disk, and
sampled combo_ids resolve in the dictionary. It also **reports** the populated-cell
count N, the COG size, and what an equivalent sparse-Parquet table would be, so
the COG-vs-table storage choice can be revisited empirically once N is known.

## 5. Hazard (reference, no copies)

GLOFAS hazard tiles stay hosted by JRC. Author a thin reference manifest so hazard
is citable/version-pinned without re-hosting:

```bash
uv run euroflood build-hazard-manifest     # writes hazard/hazard_manifest.json
```

## 6. Publish the bundle

The bundle + manifests are now ready to publish. `euroflood publish` uploads them to
**Source Cooperative** (the `/vsicurl`-queryable live host) and, optionally, mints a
citable **Zenodo** DOI, stamping both into the manifest's `source_urls`:

```bash
euroflood publish --source-coop --version 1.0.0   # live host (needs AWS temp creds in .env)
euroflood publish --zenodo --version 1.0.0        # + citable DOI (needs ZENODO_TOKEN)
```

See [RELEASING.md](https://github.com/cisgroup/euroflood/blob/main/RELEASING.md) for the
full release procedure (library → PyPI, index → Source Cooperative / Zenodo).
