"""Tests for euroflood._data (hosted-index URL resolution + pooch fetch)."""

from pathlib import Path

import euroflood._data as _data
from euroflood.config import Settings


def test_resolve_base_url_prefers_configured(monkeypatch, tmp_path):
    monkeypatch.setattr(_data, "DEFAULT_INDEX_BASE_URL", "https://baked/idx")
    s = Settings(cache_dir=tmp_path, index_base_url="https://cfg/idx")
    assert _data.resolve_base_url(s) == "https://cfg/idx"


def test_resolve_base_url_falls_back_to_baked_default(monkeypatch, tmp_path):
    monkeypatch.setattr(_data, "DEFAULT_INDEX_BASE_URL", "https://baked/idx")
    s = Settings(cache_dir=tmp_path, index_base_url=None)
    assert _data.resolve_base_url(s) == "https://baked/idx"


def test_resolve_base_url_uses_baked_published_default(tmp_path):
    # DEFAULT_INDEX_BASE_URL now ships baked to the published Source Cooperative prefix.
    s = Settings(cache_dir=tmp_path, index_base_url=None)
    assert _data.resolve_base_url(s) == _data.DEFAULT_INDEX_BASE_URL
    assert _data.DEFAULT_INDEX_BASE_URL == (
        "https://data.source.coop/hackl/euroflood-index/v1.0.0"
    )


def test_fetch_file_passes_url_and_sha_to_pooch(mocker, tmp_path):
    ret = mocker.patch(
        "euroflood._data.pooch.retrieve", return_value=str(tmp_path / "f.parquet")
    )
    out = _data.fetch_file(
        "https://host/idx/", "f.parquet", tmp_path, known_hash="deadbeef"
    )
    kw = ret.call_args.kwargs
    assert kw["url"] == "https://host/idx/f.parquet"
    assert kw["known_hash"] == "sha256:deadbeef"
    assert kw["fname"] == "f.parquet"
    assert kw["path"] == tmp_path
    assert out == Path(tmp_path / "f.parquet")


def test_fetch_file_none_hash_skips_verification(mocker, tmp_path):
    ret = mocker.patch(
        "euroflood._data.pooch.retrieve", return_value=str(tmp_path / "m.json")
    )
    _data.fetch_file("https://host/idx", "m.json", tmp_path, known_hash=None)
    assert ret.call_args.kwargs["known_hash"] is None
