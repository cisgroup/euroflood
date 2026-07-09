"""Tests for IndexRepository (auto/local/remote + /vsicurl + mirror-on-first-use)."""

import json

from euroflood.services.index_repository import IndexRepository


def test_local_mode_source_is_cache_path(mock_settings):
    """Local mode returns the cache path and flags a missing COG as hard-missing."""
    repo = IndexRepository(settings=mock_settings)  # conftest defaults to "local"
    assert not repo.is_remote
    assert repo.index_source() == str(mock_settings.get_index_tif_path())
    assert repo.local_index_missing() is True
    mock_settings.get_index_tif_path().write_bytes(b"x")
    assert repo.local_index_missing() is False


def test_remote_source_is_vsicurl_when_absent(mock_settings):
    """Remote mode streams the COG via /vsicurl when it isn't mirrored locally."""
    mock_settings.index_mode = "remote"
    mock_settings.index_base_url = "https://host/idx/"
    repo = IndexRepository(settings=mock_settings)
    assert repo.is_remote
    assert repo.index_source() == (
        f"/vsicurl/https://host/idx/{mock_settings.index_filename}"
    )
    assert repo.local_index_missing() is False  # available remotely


def test_remote_prefers_local_when_mirrored(mock_settings):
    """A mirrored COG is preferred over /vsicurl even in remote mode."""
    mock_settings.index_mode = "remote"
    mock_settings.index_base_url = "https://host/idx"
    mock_settings.get_index_tif_path().write_bytes(b"x")
    repo = IndexRepository(settings=mock_settings)
    assert repo.index_source() == str(mock_settings.get_index_tif_path())


def test_auto_mode_uses_baked_default_url(mock_settings, mocker):
    """In 'auto', a baked DEFAULT_INDEX_BASE_URL is used with no env config."""
    mock_settings.index_mode = "auto"
    mock_settings.index_base_url = None
    mocker.patch("euroflood._data.DEFAULT_INDEX_BASE_URL", "https://cdn/idx/v1")
    repo = IndexRepository(settings=mock_settings)
    assert repo.is_remote
    assert (
        repo.index_source()
        == f"/vsicurl/https://cdn/idx/v1/{mock_settings.index_filename}"
    )


def test_auto_mode_prefers_local_bundle(mock_settings, mocker):
    """In 'auto', a present local bundle wins over the hosted index."""
    mock_settings.index_mode = "auto"
    mocker.patch("euroflood._data.DEFAULT_INDEX_BASE_URL", "https://cdn/idx/v1")
    mock_settings.get_index_tif_path().write_bytes(b"x")
    repo = IndexRepository(settings=mock_settings)
    assert repo.index_source() == str(mock_settings.get_index_tif_path())


def test_auto_without_url_is_local(mock_settings, mocker):
    """In 'auto' with no configured/baked URL, behaviour collapses to local."""
    mock_settings.index_mode = "auto"
    mock_settings.index_base_url = None
    mocker.patch("euroflood._data.DEFAULT_INDEX_BASE_URL", None)  # simulate unpublished
    repo = IndexRepository(settings=mock_settings)
    assert not repo.is_remote
    assert repo.local_index_missing() is True


def test_ensure_tables_noop_local(mock_settings, mocker):
    """Local mode never touches the network."""
    spy = mocker.patch("requests.get")
    IndexRepository(settings=mock_settings).ensure_tables()
    spy.assert_not_called()


def _mock_remote(mocker, manifest):
    """Patch the manifest fetch (requests) + the per-file fetch (pooch via fetch_file).

    ``fetch_file`` writes a placeholder into the cache and records the (rel, hash) it
    was asked to fetch, so tests can assert which files were pulled with which hash.
    """
    mocker.patch(
        "requests.get",
        return_value=mocker.Mock(
            content=json.dumps(manifest).encode(),
            raise_for_status=mocker.Mock(),
        ),
    )
    calls: list[tuple[str, str | None]] = []

    def fake_fetch(base, rel, dest_dir, *, known_hash):
        calls.append((rel, known_hash))
        dest = dest_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"data")
        return dest

    mocker.patch(
        "euroflood.services.index_repository.fetch_file", side_effect=fake_fetch
    )
    return calls


def test_mirror_full_downloads_all_from_manifest(mock_settings, mocker):
    """A full mirror pulls every file in the manifest (COG included), hash-verified."""
    mock_settings.index_mode = "remote"
    mock_settings.index_base_url = "https://host/idx"
    manifest = {
        "files": {
            "europe_flood_index.tif": {"sha256": "aaa"},
            "events.parquet": {"sha256": "bbb"},
            "dictionary_meta.json": {"sha256": "ccc"},
            "flood_dictionary.parquet": {"sha256": "ddd"},
        }
    }
    calls = _mock_remote(mocker, manifest)
    IndexRepository(settings=mock_settings).mirror(include_cog=True)

    assert mock_settings.get_events_path().exists()
    assert mock_settings.get_index_tif_path().exists()
    assert mock_settings.get_dictionary_parquet_path().exists()
    # Hashes from the manifest are threaded through for integrity verification.
    assert ("flood_dictionary.parquet", "ddd") in calls
    assert ("europe_flood_index.tif", "aaa") in calls


def test_mirror_tables_only_skips_cog(mock_settings, mocker):
    """--tables-only mirrors the small tables and leaves the COG to stream."""
    mock_settings.index_mode = "remote"
    mock_settings.index_base_url = "https://host/idx"
    manifest = {
        "files": {
            "europe_flood_index.tif": {"sha256": "aaa"},
            "events.parquet": {"sha256": "bbb"},
        }
    }
    calls = _mock_remote(mocker, manifest)
    IndexRepository(settings=mock_settings).mirror(include_cog=False)

    assert mock_settings.get_events_path().exists()
    assert not mock_settings.get_index_tif_path().exists()
    assert [rel for rel, _ in calls] == ["events.parquet"]


def test_open_index_caches_and_reuses(mock_settings, mocker):
    """open_index opens the COG once and reuses it; close_cached_datasets releases it."""
    from euroflood.services.index_repository import close_cached_datasets

    mock_settings.get_index_tif_path().write_bytes(b"x")  # a local source path
    fake = mocker.MagicMock(closed=False)
    opened = mocker.patch("rasterio.open", return_value=fake)
    repo = IndexRepository(settings=mock_settings)

    s1 = repo.open_index()
    s2 = repo.open_index()
    assert s1 is s2  # reused, not re-opened
    opened.assert_called_once()

    close_cached_datasets()
    fake.close.assert_called_once()


def test_open_index_reopens_a_closed_dataset(mock_settings, mocker):
    mock_settings.get_index_tif_path().write_bytes(b"x")
    closed, fresh = mocker.MagicMock(closed=True), mocker.MagicMock(closed=False)
    mocker.patch("rasterio.open", side_effect=[closed, fresh])
    repo = IndexRepository(settings=mock_settings)

    repo.open_index()  # caches `closed` (which reports .closed == True)
    assert repo.open_index() is fresh  # stale handle -> re-open
