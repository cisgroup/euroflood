#!/bin/bash
# =============================================================================
# EuroFlood on Della — STEP 3: build the index COG + dictionary + manifest (OFFLINE).
#
# Runs AFTER della_ingest. Single PROCESS (no worker pool), thread-parallel: DuckDB
# aggregates the Parquet lake (SET threads) + the GDAL COG driver uses
# NUM_THREADS=ALL_CPUS. Profiled on the full archive (@32c and @8c,
# 2026-07-02): only the brief DuckDB aggregation is core-hungry (14% CPU eff at 32,
# 28.5% at 8); the raster write + dictionary export -- the bulk -- are I/O-bound. So
# right-sized to 4 cpus. Peak ~73-80 GB, wall ~2-3 min. Doctor validates the bundle.
#
# Submit:
#   cd /scratch/gpfs/<GROUP>/$USER/euroflood && mkdir -p logs
#   sbatch scripts/slurm/della_build_index.sbatch
# -----------------------------------------------------------------------------
#SBATCH --job-name=euroflood-build-index
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4      # Profiled 28.5% CPU eff at 8 cores (~2.3 used) --
                              # only the brief DuckDB aggregate is core-hungry; 4 fits with headroom.
#SBATCH --mem=112G             # Peak 72.6 GB (DuckDB aggregate of 211 M pts -> 82.5 M cells):
                              # ~1.5x margin, fits a 190 GB node. Data-dependent (grows
                              # with the archive); memory_limit (below) sits UNDER this so DuckDB
                              # spills to disk instead of getting OOM-killed.
#SBATCH --time=00:20:00        # Ran 2:50 at 8 cores; ~6x margin at 4. <61min -> 'test' QOS.
#SBATCH --mail-type=BEGIN,END,FAIL
#SBATCH --mail-user=<you>@princeton.edu

set -euo pipefail
[[ "${SLURM_DEBUG:-0}" == "1" ]] && set -x
cd "$SLURM_SUBMIT_DIR"
[[ -d logs ]] || { echo "logs/ must exist — run: mkdir -p logs" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
export EUROFLOOD_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/euroflood"
export UV_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/uv-cache"
export UV_OFFLINE=1

[[ -d "$EUROFLOOD_CACHE_DIR/parquet" ]] || {
    echo "no Parquet lake — run scripts/slurm/della_ingest.sbatch first" >&2
    exit 1
}

# Single process: let DuckDB + the GDAL COG driver use the whole allocation.
export EUROFLOOD_INDEX_THREADS="$SLURM_CPUS_PER_TASK"
export EUROFLOOD_INDEX_MEMORY_LIMIT=96GB    # < --mem so DuckDB spills to disk instead of OOM;
                                            # 1.3x over the observed 72.6 GB working set.
export GDAL_NUM_THREADS=ALL_CPUS
export GDAL_CACHEMAX=4096       # MB

uv sync --frozen --offline
echo "node=$(hostname) commit=$(git rev-parse --short HEAD 2>/dev/null || echo n/a) start=$(date -u +%FT%TZ)"

uv run euroflood build-index   # export (materialize -> sparse COG -> dictionary -> manifest) + doctor

echo "build-index done in ${SECONDS}s -> $EUROFLOOD_CACHE_DIR/europe_flood_index.tif"
