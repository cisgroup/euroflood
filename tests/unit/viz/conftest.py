"""Fixtures for the viz tests: real historic/hazard FloodFrames over a tiny index."""

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from euroflood.core.manifest import write_manifest
from euroflood.pipelines.discovery import FloodFrame, _make_frame
from euroflood.schemas import DICTIONARY_SCHEMA_VERSION


@pytest.fixture
def patch_download(mocker):
    """Patch ``FloodFrame.download`` to attach given raster paths and return self.

    Mirrors the real (post-download) frame — the depth view reads ``.files`` — so
    tests can supply fixture rasters without hitting the network. Paths are cycled
    across the frame's rows.
    """

    def _apply(*paths):
        names = [str(p) for p in paths]

        def _download(self, *_args, **_kwargs):
            self["path"] = [names[i % len(names)] for i in range(len(self))]
            return self

        mocker.patch.object(FloodFrame, "download", _download)

    return _apply


@pytest.fixture(autouse=True)
def _close_matplotlib_figures():
    """Close any matplotlib figures after each test to bound memory."""
    yield
    try:
        import matplotlib.pyplot as plt

        plt.close("all")
    except ImportError:
        pass


# The tiny index: a 10x10 combo raster with combo_id 1 on the diagonal, at
# 10E/50N, 0.01deg pixels. combo 1 -> flood event 10.
_TRANSFORM = from_origin(10.0, 50.0, 0.01, 0.01)
_ROI = box(9.99, 49.89, 10.11, 50.01)  # fully covers the raster


def _build_index(settings):
    """Write the dictionary (JSON) + index COG + manifest into the cache."""
    dic = {
        "schema_version": DICTIONARY_SCHEMA_VERSION,
        "combos": {
            "1": {
                "flood_ids": [10],
                "events": [
                    {
                        "global_id": 10,
                        "start_date": "2020-05-01",
                        "end_date": "2020-05-09",
                        "year": "2020",
                        "cluster_id": "3",
                        "filename": "WD_MERGE_2020_a.tif",
                        "download_url": "http://mock/2020/WD_MERGE_2020_a.tif",
                    }
                ],
            }
        },
    }
    settings.get_dictionary_path().write_text(json.dumps(dic))

    data = np.zeros((10, 10), dtype="uint32")
    np.fill_diagonal(data, 1)
    with rasterio.open(
        settings.get_index_tif_path(),
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="uint32",
        crs="EPSG:4326",
        transform=_TRANSFORM,
        nodata=0,
    ) as dst:
        dst.write(data, 1)
    write_manifest(settings.get_manifest_path())


@pytest.fixture
def historic_frame(mock_settings):
    """A 1-event historic FloodFrame whose ROI covers the tiny index."""
    _build_index(mock_settings)
    row = {
        "collection": "historic",
        "event_id": 10,
        "date": "2020-05-01",
        "year": 2020,
        "end_date": "2020-05-09",
        "cluster_id": "3",
        "filename": "WD_MERGE_2020_a.tif",
        "download_url": "http://mock/2020/WD_MERGE_2020_a.tif",
        "area_km2": 8.0,
        "geometry": _ROI,
    }
    return _make_frame([row], mock_settings)


@pytest.fixture
def two_event_frame(mock_settings):
    """A 2-event historic frame: event 10 floods the top half, event 11 the bottom."""
    dic = {
        "schema_version": DICTIONARY_SCHEMA_VERSION,
        "combos": {
            "1": {
                "flood_ids": [10],
                "events": [
                    {
                        "global_id": 10,
                        "start_date": "2020-05-01",
                        "end_date": "2020-05-09",
                        "year": "2020",
                        "cluster_id": "3",
                        "filename": "a.tif",
                        "download_url": "http://mock/a.tif",
                    }
                ],
            },
            "2": {
                "flood_ids": [11],
                "events": [
                    {
                        "global_id": 11,
                        "start_date": "2021-07-01",
                        "end_date": "2021-07-05",
                        "year": "2021",
                        "cluster_id": "4",
                        "filename": "b.tif",
                        "download_url": "http://mock/b.tif",
                    }
                ],
            },
        },
    }
    mock_settings.get_dictionary_path().write_text(json.dumps(dic))
    data = np.zeros((10, 10), dtype="uint32")
    data[:5, :] = 1  # top half -> combo 1 -> event 10
    data[5:, :] = 2  # bottom half -> combo 2 -> event 11
    with rasterio.open(
        mock_settings.get_index_tif_path(),
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="uint32",
        crs="EPSG:4326",
        transform=_TRANSFORM,
        nodata=0,
    ) as dst:
        dst.write(data, 1)
    write_manifest(mock_settings.get_manifest_path())
    rows = [
        {
            "collection": "historic",
            "event_id": 10,
            "date": "2020-05-01",
            "year": 2020,
            "end_date": "2020-05-09",
            "cluster_id": "3",
            "filename": "a.tif",
            "download_url": "http://mock/a.tif",
            "area_km2": 8.0,
            "geometry": _ROI,
        },
        {
            "collection": "historic",
            "event_id": 11,
            "date": "2021-07-01",
            "year": 2021,
            "end_date": "2021-07-05",
            "cluster_id": "4",
            "filename": "b.tif",
            "download_url": "http://mock/b.tif",
            "area_km2": 5.0,
            "geometry": _ROI,
        },
    ]
    return _make_frame(rows, mock_settings)


@pytest.fixture
def multicount_frame(mock_settings):
    """A frame whose ROI has a combo flooded by 3 events (recurrence max = 3)."""
    dic = {
        "schema_version": DICTIONARY_SCHEMA_VERSION,
        "combos": {
            "1": {"flood_ids": [10, 11, 12], "events": []},  # top half: 3 floods
            "2": {"flood_ids": [10], "events": []},  # bottom half: 1 flood
        },
    }
    mock_settings.get_dictionary_path().write_text(json.dumps(dic))
    data = np.zeros((10, 10), dtype="uint32")
    data[:5, :] = 1
    data[5:, :] = 2
    with rasterio.open(
        mock_settings.get_index_tif_path(),
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="uint32",
        crs="EPSG:4326",
        transform=_TRANSFORM,
        nodata=0,
    ) as dst:
        dst.write(data, 1)
    write_manifest(mock_settings.get_manifest_path())
    row = {
        "collection": "historic",
        "event_id": 10,
        "date": "2020-05-01",
        "year": 2020,
        "end_date": "2020-05-09",
        "cluster_id": "3",
        "filename": "a.tif",
        "download_url": "http://mock/a.tif",
        "area_km2": 8.0,
        "geometry": _ROI,
    }
    return _make_frame([row], mock_settings)


@pytest.fixture
def hazard_frame(mock_settings):
    """A 1-layer hazard FloodFrame (different schema; no combo index)."""
    row = {
        "collection": "hazard",
        "return_period": 100,
        "model_version": "v2.1.2",
        "n_tiles": 2,
        "filename": "hazard_RP100.tif",
        "area_km2": 891.2,
        "geometry": _ROI,
    }
    return _make_frame([row], mock_settings)


@pytest.fixture
def make_historic_frame(mock_settings):
    """Factory: build an n-row historic frame (for the depth guardrail test)."""

    def _factory(n):
        rows = [
            {
                "collection": "historic",
                "event_id": i,
                "date": "2020-05-01",
                "year": 2020,
                "end_date": None,
                "cluster_id": "3",
                "filename": f"{i}.tif",
                "download_url": "http://mock/x",
                "area_km2": 1.0,
                "geometry": _ROI,
            }
            for i in range(n)
        ]
        return _make_frame(rows, mock_settings)

    return _factory
