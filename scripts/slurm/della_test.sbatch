#!/bin/bash
# =============================================================================
# EuroFlood on Della — TEST run: validate the offline flow before the big jobs.
#
# A quick end-to-end smoke on a SMALL subset, in the `test` QOS (--time < 61 min,
# one node). First stage a few tiles on a node WITH internet (separate test cache
# so it doesn't touch the real run):
#   ssh <NetID>@della-vis1.princeton.edu
#   cd /scratch/gpfs/<GROUP>/$USER/euroflood
#   EUROFLOOD_CACHE_DIR=/scratch/gpfs/<GROUP>/$USER/euroflood-test \
#       uv run euroflood mirror --update --limit 5      # ~50 MB
# Preview anything first with --dry-run, e.g.:
#   uv run euroflood ingest --limit 5 --dry-run
# Then from a login node:
#   mkdir -p logs && sbatch scripts/slurm/della_test.sbatch
# Tune the subset size with:  sbatch --export=ALL,LIMIT=10 scripts/slurm/della_test.sbatch
# -----------------------------------------------------------------------------
#SBATCH --job-name=euroflood-test
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=00:30:00         # < 61 min -> 'test' QOS (one node, quick validation)
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=<you>@princeton.edu

set -euo pipefail
[[ "${SLURM_DEBUG:-0}" == "1" ]] && set -x
cd "$SLURM_SUBMIT_DIR"
[[ -d logs ]] || { echo "logs/ must exist — run: mkdir -p logs" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
export EUROFLOOD_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/euroflood-test"  # separate test cache
export UV_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/uv-cache"
export UV_OFFLINE=1
export EUROFLOOD_MAX_WORKERS_CPU="$SLURM_CPUS_PER_TASK"
export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1 GDAL_NUM_THREADS=1

LIMIT="${LIMIT:-5}"

# Preflight: the small subset must already be mirrored (no internet on compute).
[[ -f "$EUROFLOOD_CACHE_DIR/inventory.csv" ]] || {
    echo "no inventory in $EUROFLOOD_CACHE_DIR — stage a subset on della-vis first:" >&2
    echo "  EUROFLOOD_CACHE_DIR=$EUROFLOOD_CACHE_DIR uv run euroflood mirror --update --limit $LIMIT" >&2
    exit 1
}

uv sync --frozen --offline
uv run euroflood ingest --limit "$LIMIT"     # process the small subset (offline)
uv run euroflood build-index                 # build a tiny index + doctor gate

echo "test run OK in ${SECONDS}s -> $EUROFLOOD_CACHE_DIR/europe_flood_index.tif"
