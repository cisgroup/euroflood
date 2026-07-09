"""Opt-in tests against the LIVE published index (real network).

Skipped by default so offline CI never touches the network; enable with::

    EUROFLOOD_RUN_ONLINE_TESTS=1 uv run pytest -m online

They read the baked ``DEFAULT_INDEX_BASE_URL`` and exercise the exact remote path a
fresh ``pip install euroflood`` uses (``/vsicurl`` COG stream + mirrored tables), by
**bbox** so there is no live geocoder dependency.
"""

import os

import pytest

import euroflood as ef
from euroflood._data import DEFAULT_INDEX_BASE_URL
from euroflood.pipelines.verify import verify_published

pytestmark = [
    pytest.mark.online,
    pytest.mark.skipif(
        not os.environ.get("EUROFLOOD_RUN_ONLINE_TESTS"),
        reason="set EUROFLOOD_RUN_ONLINE_TESTS=1 to run live-index tests",
    ),
]

_ZUTPHEN = (
    6.14,
    52.09,
    6.27,
    52.17,
)  # IJssel; carries real events in the published index


def test_verify_published_live_is_green():
    report = verify_published(DEFAULT_INDEX_BASE_URL)
    assert report.ok, report.summary()
    assert report.n_events and report.n_events > 0


def test_floods_query_over_published_index(mock_settings):
    mock_settings.index_mode = "remote"
    mock_settings.index_base_url = DEFAULT_INDEX_BASE_URL
    mock_settings.timeout_seconds = 60
    cat = ef.floods(bbox=_ZUTPHEN, settings=mock_settings)
    assert len(cat) > 0
    assert str(cat.iloc[0]["filename"]).startswith("WD_MERGE_")
