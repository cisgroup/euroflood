"""Tests for configuration management."""

from pathlib import Path

from euroflood.config import Settings


def test_default_config():
    """Test defaults are sensible."""
    # We instantiate a fresh Settings object to avoid the mocked global one
    # Note: This might pick up real env vars if set in the shell running pytest
    conf = Settings()
    assert conf.log_level == "INFO"
    assert conf.retries == 3


def test_env_var_override(monkeypatch):
    """Test that environment variables override defaults."""
    monkeypatch.setenv("EUROFLOOD_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EUROFLOOD_MAX_WORKERS_DL", "99")

    conf = Settings()
    assert conf.log_level == "DEBUG"
    assert conf.max_workers_dl == 99


def test_ensure_dirs(tmp_path):
    """Test directory creation."""
    conf = Settings(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    conf.ensure_dirs()

    assert (tmp_path / "c").exists()
    assert (tmp_path / "o").exists()


def test_cache_path_helpers_are_under_cache_dir(tmp_path):
    """The inventory/index/dictionary/manifest helpers resolve under cache_dir (132,140,148,156)."""
    conf = Settings(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    cache = tmp_path / "c"
    assert conf.get_inventory_path() == cache / conf.inventory_filename
    assert conf.get_index_tif_path() == cache / conf.index_filename
    assert conf.get_dictionary_path() == cache / conf.dictionary_filename
    assert conf.get_manifest_path() == cache / conf.manifest_filename
    for p in (
        conf.get_inventory_path(),
        conf.get_index_tif_path(),
        conf.get_dictionary_path(),
        conf.get_manifest_path(),
    ):
        assert p.parent == cache


def test_hazard_and_dictionary_path_helpers(tmp_path):
    """Hazard + Parquet-dictionary path helpers compose correctly (174,178,182,186)."""
    conf = Settings(cache_dir=tmp_path / "c", output_dir=tmp_path / "o")
    cache = tmp_path / "c"
    hazard = cache / "hazard"
    assert conf.get_hazard_dir() == hazard
    assert conf.get_hazard_index_path() == hazard / conf.hazard_index_filename
    assert conf.get_hazard_tiles_dir() == hazard / "tiles"
    assert conf.get_hazard_manifest_path() == hazard / conf.hazard_manifest_filename
    assert (
        conf.get_dictionary_parquet_path() == cache / conf.dictionary_parquet_filename
    )
    assert conf.get_dictionary_meta_path() == cache / conf.dictionary_meta_filename


def test_hazard_index_path_override_wins(tmp_path):
    """An explicit hazard_index_path overrides the default cache location."""
    override = tmp_path / "custom" / "tile_extents.geojson"
    conf = Settings(
        cache_dir=tmp_path / "c", output_dir=tmp_path / "o", hazard_index_path=override
    )
    assert conf.get_hazard_index_path() == override
    assert (
        conf.get_hazard_index_path()
        != conf.get_hazard_dir() / conf.hazard_index_filename
    )


def test_path_env_var_override_coerces_to_path(monkeypatch, tmp_path):
    """A string path from an env var is coerced to Path (validate_assignment)."""
    monkeypatch.setenv("EUROFLOOD_CACHE_DIR", str(tmp_path / "envcache"))
    conf = Settings()
    assert isinstance(conf.cache_dir, Path)
    assert conf.cache_dir == tmp_path / "envcache"
    assert conf.get_inventory_path().parent == tmp_path / "envcache"


def test_max_plausible_depth_env_override(monkeypatch):
    """The optional plausibility cap is read from the environment as a float."""
    monkeypatch.setenv("EUROFLOOD_MAX_PLAUSIBLE_DEPTH", "12.5")
    conf = Settings()
    assert conf.max_plausible_depth == 12.5
