"""Opt-in tests: mirror real GLOFAS hazard tiles, then use them fully offline.

Skipped by default; enable with::

    EUROFLOOD_RUN_ONLINE_TESTS=1 uv run pytest -m online

Uses a tiny 1-tile bbox at a single return period so the download stays small, and
selects by **bbox** (no live geocoder).
"""

import os

import pytest
import rasterio

import euroflood as ef
from euroflood.pipelines.hazard import mirror_hazard, verify_hazard_mirror

pytestmark = [
    pytest.mark.online,
    pytest.mark.skipif(
        not os.environ.get("EUROFLOOD_RUN_ONLINE_TESTS"),
        reason="set EUROFLOOD_RUN_ONLINE_TESTS=1 to run live-hazard tests",
    ),
]

# A small bbox well inside a single 10x10-degree GLOFAS tile (NL / IJssel).
_TINY = (6.10, 52.00, 6.30, 52.20)


def test_mirror_then_query_offline(mock_settings):
    mock_settings.timeout_seconds = 60  # JRC can be slow

    # Plan first so the test fails fast if the ROI ever grows beyond one tile.
    plan = mirror_hazard(bbox=_TINY, return_period=10, dry_run=True)
    assert plan.n_expected == 1

    # Real region-scoped mirror from live JRC.
    res = mirror_hazard(bbox=_TINY, return_period=10)
    assert res.downloaded == 1 and not res.missing

    # Deep verify passes (checksums match the just-written ledger).
    assert verify_hazard_mirror(bbox=_TINY, return_period=10, deep=True).ok

    # Fully offline download reads only the cache.
    mock_settings.offline = True
    out = ef.hazard(bbox=_TINY, return_period=10).download(mock_settings.output_dir)
    assert len(out.files) == 1
    with rasterio.open(out.files[0]) as src:
        assert src.tags()["EUROFLOOD_RETURN_PERIOD"] == "10"
