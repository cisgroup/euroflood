#!/bin/bash
# =============================================================================
# EuroFlood on Della — STEP 2: process the mirrored tiles into Parquet (OFFLINE).
#
# Runs AFTER della_mirror.sh. Compute nodes have no internet, but ingest only
# CACHE-HITS the already-downloaded tiles (no network) and processes them. The
# ledger makes it resumable: resubmit and finished files are skipped.
#
# Submit (logs/ must exist first):
#   cd /scratch/gpfs/<GROUP>/$USER/euroflood
#   mkdir -p logs
#   sbatch scripts/slurm/della_ingest.sbatch
# Status:  squeue -u $USER ;  tail -f logs/euroflood-ingest-*.out
#
# Validated 2026-07-01: 3280 tiles -> 211 M points in 5:50 wall,
# peak 13.98 GB across 32 workers, 0 failures. Check `sacct`/`seff`, not the .out
# (the pipeline exits 0 even on mass failure).
# -----------------------------------------------------------------------------
#SBATCH --job-name=euroflood-ingest
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=64G              # Real run peaked 13.98 GB across 32 workers; the
                               # hard ceiling if all 32 hit the densest tile at once is
                               # 32 x ~1.43 GB ~= 46 GB (measured on Della, getrusage). 64 G
                               # clears that pathological bound with margin (4.5x the observed
                               # peak) and fits any node. Windowed read + GDAL_CACHEMAX (below)
                               # make this safe; pre-fix whole-tile reads OOM'd at 128 G & 256 G.
#SBATCH --time=00:20:00         # Real run 5:50; ~3x margin for a full 3280-tile run.
                               # <61 min -> 'test' QOS (fast; proven to allow 32c). 32 cores stay:
                               # ingest ran 81% CPU-efficient, so they speed it up, not waste.
#SBATCH --mail-type=BEGIN,END,FAIL,TIME_LIMIT_90
#SBATCH --mail-user=<you>@princeton.edu

set -euo pipefail
[[ "${SLURM_DEBUG:-0}" == "1" ]] && set -x
cd "$SLURM_SUBMIT_DIR"
[[ -d logs ]] || { echo "logs/ must exist — run: mkdir -p logs" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
export EUROFLOOD_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/euroflood"
export UV_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/uv-cache"
export UV_OFFLINE=1            # no internet here — use the cache warmed by della_mirror.sh

# Preflight: the inventory + downloads must already be staged (no internet to fetch them).
[[ -f "$EUROFLOOD_CACHE_DIR/inventory.csv" ]] || {
    echo "inventory.csv missing — run scripts/slurm/della_mirror.sh on della-vis first" >&2
    exit 1
}

# Parallelism comes from EuroFlood's process pool — match it to the SLURM
# allocation (the os.cpu_count() default would oversubscribe a shared node) and
# pin every numeric/GDAL library to one thread so workers don't fight.
export EUROFLOOD_MAX_WORKERS_CPU="$SLURM_CPUS_PER_TASK"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 GDAL_NUM_THREADS=1
# GDAL's block cache is PER PROCESS and defaults to a fraction of node RAM, so 32
# workers each cache ~8 GB of decompressed strips -> OOM even with stripe reads.
# We read each strip once, top-to-bottom, so a tiny cache costs nothing. (MB)
export GDAL_CACHEMAX=256

uv sync --frozen --offline
echo "node=$(hostname) cpus=$SLURM_CPUS_PER_TASK commit=$(git rev-parse --short HEAD 2>/dev/null || echo n/a) start=$(date -u +%FT%TZ)"

uv run euroflood ingest        # all years; offline cache-hits + process -> Parquet

echo "ingest done in ${SECONDS}s -> $EUROFLOOD_CACHE_DIR/parquet"
