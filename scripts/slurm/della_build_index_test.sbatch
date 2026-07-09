#!/bin/bash
# =============================================================================
# EuroFlood on Della — DIAGNOSTIC: profile STEP 3 (build-index) to right-size it.
#
# della_build_index.sbatch asks for 32 cpus / 256 G / 6 h, but those are guesses.
# build-index is single-PROCESS but MULTI-THREADED (DuckDB `SET threads` for the
# ~200 M-point aggregation + the GDAL COG driver's NUM_THREADS=ALL_CPUS), so it
# *can* use all 32 cores -- but we've never measured how much it actually uses.
#
# This runs the REAL build-index on the 'test' QOS (<1 h) with instrumentation so
# we can set cpus/mem/time from actual numbers:
#   - peak RSS       : live `sstat` trace (logs/..-mem-<jid>.tsv) + /usr/bin/time
#   - wall + phases  : DuckDB aggregate -> raster write -> COG-ify -> doctor
#   - CPU efficiency : `seff <jid>` afterwards -> is 32 cores justified, or fewer?
#
# It writes the real bundle into the cache. build-index is NOT resumable but IS
# idempotent (CREATE OR REPLACE; manifest written LAST, so a TIME_LIMIT kill
# leaves no trusted half-bundle) -- if it hits the 1 h wall, just resubmit the
# real della_build_index.sbatch on 'short' QOS.
#
# Submit:  cd /scratch/gpfs/<GROUP>/$USER/euroflood && mkdir -p logs
#          sbatch scripts/slurm/della_build_index_test.sbatch
# After:   seff <jid> ; column -t logs/euroflood-buildtest-mem-<jid>.tsv
# -----------------------------------------------------------------------------
#SBATCH --job-name=euroflood-buildtest
#SBATCH --output=logs/%x-%j.out
#SBATCH --error=logs/%x-%j.err
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=256G             # generous ON PURPOSE for the probe -- we want to SEE the natural
                              # peak, not cap it. (DuckDB uses memory_limit as a ceiling and
                              # spills past it, so peak RSS here = the true working set.)
#SBATCH --time=00:55:00        # < 61 min -> 'test' QOS. If it hits the wall, it needs 'short'.
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=<you>@princeton.edu

set -euo pipefail
[[ "${SLURM_DEBUG:-0}" == "1" ]] && set -x
cd "$SLURM_SUBMIT_DIR"
[[ -d logs ]] || { echo "logs/ must exist -- run: mkdir -p logs" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
export EUROFLOOD_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/euroflood"
export UV_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/uv-cache"
export UV_OFFLINE=1

[[ -d "$EUROFLOOD_CACHE_DIR/parquet" ]] || {
    echo "no Parquet lake -- run scripts/slurm/della_ingest.sbatch first" >&2; exit 1; }

# Same knobs as the real build-index, so we measure the real config.
export EUROFLOOD_INDEX_THREADS="$SLURM_CPUS_PER_TASK"
export EUROFLOOD_INDEX_MEMORY_LIMIT=200GB
export GDAL_NUM_THREADS=ALL_CPUS
export GDAL_CACHEMAX=4096       # MB

uv sync --frozen --offline
echo "node=$(hostname) cpus=$SLURM_CPUS_PER_TASK start=$(date -u +%FT%TZ)"

# --- live peak-RSS sampler: poll the step's MaxRSS every 10 s into a trace -----
MEMLOG="logs/${SLURM_JOB_NAME}-mem-${SLURM_JOB_ID}.tsv"
printf 'elapsed_s\tMaxRSS\tAveRSS\n' > "$MEMLOG"
(
  set +e   # sstat returns non-zero until the step registers; never kill the probe
  t0=$SECONDS
  while true; do
    stats=$(sstat -j "${SLURM_JOB_ID}.batch" --noheader -P --format=MaxRSS,AveRSS 2>/dev/null | tr '|' '\t')
    printf '%s\t%s\n' "$((SECONDS - t0))" "$stats" >> "$MEMLOG"
    sleep 10
  done
) &
SAMPLER=$!
trap 'kill "$SAMPLER" 2>/dev/null || true' EXIT

# --- the real build-index; capture wall + peak RSS ---------------------------
TIMELOG="logs/${SLURM_JOB_NAME}-time-${SLURM_JOB_ID}.txt"
if [[ -x /usr/bin/time ]]; then
    /usr/bin/time -v -o "$TIMELOG" uv run euroflood build-index
else
    uv run euroflood build-index    # seff still reports peak mem + CPU efficiency
fi

kill "$SAMPLER" 2>/dev/null || true

# --- summary -----------------------------------------------------------------
echo "=== build-index done in ${SECONDS}s -> $EUROFLOOD_CACHE_DIR/europe_flood_index.tif ==="
if [[ -f "$TIMELOG" ]]; then
    echo "--- /usr/bin/time ---"
    grep -E "Maximum resident|Elapsed \(wall|Percent of CPU" "$TIMELOG" || true
fi
echo "--- phase timeline (structured log) ---"
grep -aoE '"event": "[a-z_]+".*"timestamp": "[^"]+"' \
    "logs/${SLURM_JOB_NAME}-${SLURM_JOB_ID}.err" 2>/dev/null | tail -25 || true
echo "peak-RSS trace : $MEMLOG"
echo "NEXT: run 'seff ${SLURM_JOB_ID}' for CPU efficiency + peak mem, then set"
echo "      cpus-per-task / --mem / --time on della_build_index.sbatch from the numbers."
