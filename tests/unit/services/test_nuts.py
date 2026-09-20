"""Tests for the NUTS repository (identifier -> boundary, name search); fully offline."""

import json
import shutil

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box, mapping

from euroflood.exceptions import GeocodingError, NutsError
from euroflood.services import nuts as nuts_mod
from euroflood.services.nuts import (
    NUTS_ID_PATTERN,
    REGION_COLUMNS,
    NutsRepository,
    normalize_nuts_id,
    nuts_level,
)

# A tiny NUTS-like hierarchy: two countries, all four levels, one accented name and
# one suffixed name, as in the real GISCO files.
REGIONS = [
    ("NL", 0, "Nederland", box(3.0, 50.5, 7.5, 54.0)),
    ("NL2", 1, "Oost-Nederland", box(5.0, 51.0, 7.0, 53.0)),
    ("NL21", 2, "Overijssel", box(6.0, 52.0, 7.0, 53.0)),
    ("NL22", 2, "Gelderland", box(5.0, 51.0, 7.0, 52.0)),
    ("NL225", 3, "Achterhoek", box(6.0, 51.5, 7.0, 52.0)),
    ("DE", 0, "Deutschland", box(6.0, 47.0, 15.0, 55.0)),
    ("DEA", 1, "Nordrhein-Westfalen", box(6.0, 50.0, 9.0, 52.5)),
    ("DEA2", 2, "Köln", box(6.5, 50.5, 7.5, 51.5)),
    ("DEA23", 3, "Köln, Kreisfreie Stadt", box(6.8, 50.8, 7.1, 51.0)),
]
BOUNDS = {nid: geom.bounds for nid, _, _, geom in REGIONS}


def _feature(nid, level, name, geom):
    return {
        "type": "Feature",
        "properties": {
            "NUTS_ID": nid,
            "LEVL_CODE": level,
            "CNTR_CODE": nid[:2],
            "NAME_LATN": name,
            "NUTS_NAME": name,
        },
        "geometry": mapping(geom),
    }


def _write_geojson(path, regions):
    fc = {"type": "FeatureCollection", "features": [_feature(*r) for r in regions]}
    path.write_text(json.dumps(fc))
    return path


@pytest.fixture
def all_levels_file(tmp_path):
    """One boundary file holding every level (the ``nuts_dataset_path`` shape)."""
    return _write_geojson(tmp_path / "nuts_all.geojson", REGIONS)


@pytest.fixture
def gisco_dir(tmp_path):
    """A fake GISCO distribution: per-level 01M/2024 files + the attribute CSV."""
    root = tmp_path / "gisco"
    root.mkdir()
    for level in range(4):
        _write_geojson(
            root / f"NUTS_RG_01M_2024_4326_LEVL_{level}.geojson",
            [r for r in REGIONS if r[1] == level],
        )
    rows = [
        {"CNTR_CODE": nid[:2], "NUTS_ID": nid, "NAME_LATN": name, "NUTS_NAME": name}
        for nid, _, name, _ in REGIONS
    ]
    # Eurostat's classification has extra-regio 'Z' codes without a polygon; one is
    # included to exercise the guard. GISCO 2024 also lists Ukrainian sub-regions
    # (UA11, ...) in the attribute table without publishing their boundaries.
    rows.append(
        {
            "CNTR_CODE": "NL",
            "NUTS_ID": "NLZZ",
            "NAME_LATN": "Extra-Regio NUTS 2",
            "NUTS_NAME": "Extra-Regio NUTS 2",
        }
    )
    rows.append(
        {
            "CNTR_CODE": "UA",
            "NUTS_ID": "UA11",
            "NAME_LATN": "Poltavska Oblast",
            "NUTS_NAME": "Полтавська Область",
        }
    )
    pd.DataFrame(rows).to_csv(root / "NUTS_AT_2024.csv", index=False)
    return root


@pytest.fixture
def downloader(mocker, gisco_dir, mock_settings):
    """A DownloadService stand-in that 'downloads' from the fake GISCO dir and logs calls."""
    dl = mocker.Mock()

    def fake_download(url, filename, **_kw):
        src = gisco_dir / filename
        if not src.exists():
            return None
        dest = mock_settings.cache_dir / "boundaries" / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    dl.download_file.side_effect = fake_download
    return dl


@pytest.fixture
def repo(mock_settings, downloader):
    """A repository on the download path (no override), with the fake downloader."""
    return NutsRepository(settings=mock_settings, downloader=downloader)


@pytest.fixture
def local_repo(mock_settings, all_levels_file, downloader):
    """A repository served from a single local all-levels file (no downloads)."""
    mock_settings.nuts_dataset_path = all_levels_file
    return NutsRepository(settings=mock_settings, downloader=downloader)


# --- identifiers -------------------------------------------------------------
def test_normalize_nuts_id_uppercases_and_strips():
    assert normalize_nuts_id(" nl22 ") == "NL22"
    assert normalize_nuts_id("DEA23") == "DEA23"
    assert NUTS_ID_PATTERN.match("NL")


@pytest.mark.parametrize("bad", ["NL 22", "N", "NL22ZZZ1", "", "1L22", "nl-22"])
def test_normalize_nuts_id_rejects_malformed(bad):
    with pytest.raises(NutsError, match="not a valid NUTS identifier"):
        normalize_nuts_id(bad)


def test_nuts_level_from_length():
    assert [nuts_level(i) for i in ("NL", "NL2", "NL22", "NL225")] == [0, 1, 2, 3]


def test_nuts_error_is_a_geocoding_error():
    assert issubclass(NutsError, GeocodingError)  # existing handlers, CLI exit 4


# --- geometry ------------------------------------------------------------------
def test_geometry_single_id(local_repo):
    assert local_repo.geometry("NL22").bounds == pytest.approx(BOUNDS["NL22"])
    assert local_repo.geometry("nl22").bounds == pytest.approx(BOUNDS["NL22"])


def test_geometry_list_is_the_union(local_repo):
    union = local_repo.geometry(["NL22", "NL21"])
    assert union.bounds == pytest.approx((5.0, 51.0, 7.0, 53.0))
    assert union.area == pytest.approx(
        box(*BOUNDS["NL22"]).area + box(*BOUNDS["NL21"]).area
    )


def test_geometry_mixed_levels_reads_each_level_file(repo, downloader):
    union = repo.geometry(["NL225", "DEA"])
    assert union.covers(box(*BOUNDS["NL225"])) and union.covers(box(*BOUNDS["DEA"]))
    names = [c.args[1] for c in downloader.download_file.call_args_list]
    assert "NUTS_RG_01M_2024_4326_LEVL_3.geojson" in names
    assert "NUTS_RG_01M_2024_4326_LEVL_1.geojson" in names


def test_geometry_empty_list_raises(local_repo):
    with pytest.raises(NutsError, match="at least one"):
        local_repo.geometry([])


def test_geometry_unknown_id_suggests_close_ones(repo):
    with pytest.raises(NutsError, match="Unknown NUTS identifier 'NL29'") as info:
        repo.geometry("NL29")
    msg = str(info.value)
    assert "NL21" in msg and "NL22" in msg  # its would-be siblings
    assert "EUROFLOOD_NUTS_YEAR" in msg


def test_geometry_unknown_id_without_a_parent_lists_same_level_ids(repo):
    """NL99: no NL9 parent exists, so every Dutch level-2 identifier is offered."""
    with pytest.raises(NutsError, match="Unknown NUTS identifier 'NL99'") as info:
        repo.geometry("NL99")
    assert "NL21" in str(info.value) and "NL22" in str(info.value)
    assert "DEA2" not in str(info.value)  # other countries are not suggested


def test_geometry_region_name_instead_of_id_names_the_id(repo):
    with pytest.raises(NutsError, match="region name, not a NUTS identifier") as info:
        repo.geometry("Gelderland")
    assert "nuts='NL22'" in str(info.value)
    # An id-shaped name (4 upper-case letters) gets the same hint.
    with pytest.raises(NutsError, match="nuts='DEA2'"):
        repo.geometry("KOLN")


def test_geometry_classified_but_unpublished_boundary_says_so(repo):
    """An identifier GISCO lists but ships no polygon for (UA sub-regions in 2024)."""
    with pytest.raises(NutsError, match="publishes no boundary") as info:
        repo.geometry("UA11")
    assert "Poltavska Oblast" in str(info.value)
    listed = repo.regions("UA11", geometry=True)
    assert listed["NUTS_ID"].tolist() == ["UA11"]
    assert listed.geometry.iloc[0] is None or listed.geometry.iloc[0].is_empty


def test_geometry_extra_regio_has_no_boundary(local_repo):
    with pytest.raises(NutsError, match="extra-regio"):
        local_repo.geometry("NLZZ")


# --- files: download on first use, caching, overrides, offline -----------------
def test_level_file_downloaded_once_from_gisco_url(repo, downloader, mock_settings):
    repo.geometry("NL22")
    repo.geometry("NL21")  # same level: the cached file is reused
    NutsRepository(settings=mock_settings, downloader=downloader).geometry("NL2")
    calls = downloader.download_file.call_args_list
    level_calls = [c for c in calls if "LEVL_" in c.args[1]]
    assert [c.args[1] for c in level_calls] == [
        "NUTS_RG_01M_2024_4326_LEVL_2.geojson",
        "NUTS_RG_01M_2024_4326_LEVL_1.geojson",
    ]
    assert level_calls[0].args[0] == (
        "https://gisco-services.ec.europa.eu/distribution/v2/nuts/geojson/"
        "NUTS_RG_01M_2024_4326_LEVL_2.geojson"
    )
    assert (mock_settings.cache_dir / "boundaries" / level_calls[0].args[1]).exists()


def test_year_and_scale_settings_pick_the_file(mock_settings, downloader):
    mock_settings.nuts_year = 2021
    mock_settings.nuts_scale = "03M"
    repo = NutsRepository(settings=mock_settings, downloader=downloader)
    with pytest.raises(NutsError, match="Failed to download"):  # not in the fake dir
        repo.geometry("NL22")
    url = downloader.download_file.call_args.args[0]
    assert url.endswith("geojson/NUTS_RG_03M_2021_4326_LEVL_2.geojson")


def test_dataset_path_override_serves_every_level_without_download(
    local_repo, downloader
):
    for nid in ("NL", "NL2", "NL22", "NL225"):
        assert local_repo.geometry(nid).bounds == pytest.approx(BOUNDS[nid])
    assert not local_repo.regions("Gelderland").empty  # attributes too
    downloader.download_file.assert_not_called()


def test_offline_uncached_fails_closed(mock_settings, downloader):
    mock_settings.offline = True
    repo = NutsRepository(settings=mock_settings, downloader=downloader)
    with pytest.raises(NutsError, match="EUROFLOOD_NUTS_DATASET_PATH"):
        repo.geometry("NL22")
    downloader.download_file.assert_not_called()


def test_offline_uses_a_cached_level_file(mock_settings, downloader):
    NutsRepository(settings=mock_settings, downloader=downloader).geometry("NL22")
    mock_settings.offline = True
    downloader.download_file.reset_mock()
    geom = NutsRepository(settings=mock_settings, downloader=downloader).geometry(
        "NL22"
    )
    assert geom.bounds == pytest.approx(BOUNDS["NL22"])
    downloader.download_file.assert_not_called()


def test_download_failure_raises(mock_settings, mocker):
    dl = mocker.Mock()
    dl.download_file.return_value = None
    with pytest.raises(NutsError, match="Failed to download"):
        NutsRepository(settings=mock_settings, downloader=dl).geometry("NL22")


def test_unknown_id_without_attribute_table_is_a_plain_miss(
    mock_settings, mocker, gisco_dir
):
    """Offline with only the level file cached: no suggestions, still a clear error."""
    dest = (
        mock_settings.cache_dir / "boundaries" / "NUTS_RG_01M_2024_4326_LEVL_2.geojson"
    )
    dest.parent.mkdir(parents=True)
    shutil.copyfile(gisco_dir / dest.name, dest)
    mock_settings.offline = True
    with pytest.raises(NutsError, match="Unknown NUTS identifier 'NL29'"):
        NutsRepository(settings=mock_settings, downloader=mocker.Mock()).geometry(
            "NL29"
        )


def test_boundary_file_without_nuts_id_column_raises(mock_settings, tmp_path):
    gpd.GeoDataFrame(
        {"foo": ["x"]}, geometry=[box(0, 0, 1, 1)], crs="EPSG:4326"
    ).to_file(tmp_path / "bad.geojson", driver="GeoJSON")
    mock_settings.nuts_dataset_path = tmp_path / "bad.geojson"
    with pytest.raises(NutsError, match="no NUTS_ID column"):
        NutsRepository(settings=mock_settings).geometry("NL22")


def test_boundary_file_in_another_crs_is_reprojected(mock_settings, tmp_path):
    gdf = gpd.GeoDataFrame(
        {"NUTS_ID": ["NL22"]}, geometry=[box(*BOUNDS["NL22"])], crs="EPSG:4326"
    ).to_crs(epsg=3035)
    gdf.to_file(tmp_path / "laea.gpkg", driver="GPKG")
    mock_settings.nuts_dataset_path = tmp_path / "laea.gpkg"
    geom = NutsRepository(settings=mock_settings).geometry("NL22")  # LEVL_CODE derived
    assert geom.bounds == pytest.approx(BOUNDS["NL22"], abs=1e-6)


def test_load_regions_is_cached_across_instances(
    mock_settings, all_levels_file, mocker
):
    mock_settings.nuts_dataset_path = all_levels_file
    spy = mocker.spy(gpd, "read_file")
    NutsRepository(settings=mock_settings).geometry("NL22")
    NutsRepository(settings=mock_settings).geometry("NL21")
    assert spy.call_count == 1
    assert nuts_mod._load_regions.cache_info().hits >= 1


# --- regions(): search and listing ------------------------------------------------
def test_regions_search_by_name_is_accent_and_case_insensitive(repo):
    assert repo.regions("gelderland")["NUTS_ID"].tolist() == ["NL22"]
    for q in ("Köln", "koln", "KÖLN"):
        assert repo.regions(q)["NUTS_ID"].tolist() == ["DEA2", "DEA23"], q


def test_regions_search_resolves_exonyms_and_substrings(repo):
    assert repo.regions("Cologne")["NUTS_ID"].tolist() == ["DEA2", "DEA23"]
    assert repo.regions("Nederland")["NUTS_ID"].tolist() == ["NL", "NL2"]  # substring


def test_regions_search_fuzzy_typo(repo):
    assert repo.regions("Gelderlan")["NUTS_ID"].tolist() == ["NL22"]


def test_regions_filters_by_level_and_country(repo):
    nl2 = repo.regions(country="nl", level=2)
    assert nl2["NUTS_ID"].tolist() == ["NL21", "NL22"]
    assert list(nl2.columns) == REGION_COLUMNS
    assert repo.regions(level=0)["NUTS_ID"].tolist() == ["DE", "NL"]
    assert repo.regions(country="UA")["NUTS_ID"].tolist() == ["UA11"]


def test_regions_id_query_lists_descendants(repo):
    assert repo.regions("NL2")["NUTS_ID"].tolist() == ["NL2", "NL21", "NL22", "NL225"]
    assert repo.regions("nl2", level=3)["NUTS_ID"].tolist() == ["NL225"]


def test_regions_no_match_is_an_empty_frame(repo):
    out = repo.regions("Atlantis")
    assert out.empty and list(out.columns) == REGION_COLUMNS
    assert repo.regions("Atlantis", geometry=True).empty


def test_regions_without_geometry_reads_only_the_attribute_table(repo, downloader):
    out = repo.regions("Gelderland")
    assert isinstance(out, pd.DataFrame) and not isinstance(out, gpd.GeoDataFrame)
    assert [c.args[1] for c in downloader.download_file.call_args_list] == [
        "NUTS_AT_2024.csv"
    ]


def test_regions_with_geometry_loads_only_needed_levels(repo, downloader):
    out = repo.regions("Köln", geometry=True)
    assert isinstance(out, gpd.GeoDataFrame) and out.crs.to_epsg() == 4326
    assert out.geometry.iloc[0].bounds == pytest.approx(BOUNDS["DEA2"])
    names = [c.args[1] for c in downloader.download_file.call_args_list]
    assert "NUTS_RG_01M_2024_4326_LEVL_2.geojson" in names
    assert "NUTS_RG_01M_2024_4326_LEVL_3.geojson" in names
    assert "NUTS_RG_01M_2024_4326_LEVL_0.geojson" not in names


def test_regions_omit_extra_regio_codes(repo):
    """'Z' codes are statistical placeholders with no territory: never listed."""
    assert repo.regions("NLZZ").empty
    assert repo.regions("Extra-Regio").empty
    assert "NLZZ" not in repo.regions(country="NL").NUTS_ID.tolist()


# --- the public helper ---------------------------------------------------------
def test_public_nuts_helper_uses_the_repository(mock_settings, all_levels_file):
    import euroflood as ef

    mock_settings.nuts_dataset_path = all_levels_file
    assert ef.nuts("Gelderland")["NUTS_ID"].tolist() == ["NL22"]
    geo = ef.nuts(country="NL", level=2, geometry=True)
    assert isinstance(geo, gpd.GeoDataFrame)
    assert geo["NUTS_ID"].tolist() == ["NL21", "NL22"]
    assert geo.crs.to_epsg() == 4326
