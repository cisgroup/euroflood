#!/bin/bash
# =============================================================================
# EuroFlood on Della — STEP 1: mirror the source tiles (DOWNLOAD ONLY).
#
# Della COMPUTE nodes have NO internet, so the download cannot run in a SLURM
# job. Run this on a VISUALIZATION node (della-vis1 / della-vis2), which has
# internet and is RC's recommended place for large downloads. It also warms the
# uv cache + builds the venv so the offline compute jobs can run.
#
#   ssh <NetID>@della-vis1.princeton.edu
#   cd /scratch/gpfs/<GROUP>/$USER/euroflood
#   tmux new -s mirror          # so the ~35 GB pull survives a disconnect
#   bash scripts/slurm/della_mirror.sh
#
# Resumable: re-run anytime (present files are skipped). --verify (set below)
# re-downloads any cached file whose size != the JRC listing.
# =============================================================================
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"                       # uv installs here
export EUROFLOOD_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/euroflood"
export UV_CACHE_DIR="/scratch/gpfs/<GROUP>/$USER/uv-cache"  # keep off the small /home
export EUROFLOOD_MAX_WORKERS_DL=16                         # parallel download threads

mkdir -p "$EUROFLOOD_CACHE_DIR"

uv --version
uv sync                                                    # ONLINE: warms cache + builds .venv
uv run python -c "import euroflood; print('euroflood ok')"

# Scrape the inventory + download ALL source tiles (~35 GB), verifying sizes.
uv run euroflood mirror --update --verify

echo "Mirror complete -> $EUROFLOOD_CACHE_DIR/downloads"
echo "Next: submit the offline compute jobs (della_ingest.sbatch, then della_build_index.sbatch)."
