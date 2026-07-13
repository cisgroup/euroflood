"""Tests for euroflood._data (hosted-index URL resolution + pooch fetch)."""

from pathlib import Path

import pytest

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


# --- transient-host retry (Source Cooperative intermittently 500s) ----------
def _response(status: int, content: bytes = b"{}"):
    """A minimal requests.Response stand-in with a real raise_for_status()."""
    import requests

    r = requests.Response()
    r.status_code = status
    r._content = content
    r.url = "https://host/idx/manifest.json"
    return r


def test_http_get_retries_transient_5xx_then_succeeds(mocker, tmp_path):
    """A 500 from the index host is retried, not fatal (the live-tutorial failure)."""
    mocker.patch("time.sleep")  # skip tenacity's backoff
    get = mocker.patch(
        "euroflood._data.requests.get",
        side_effect=[_response(500), _response(503), _response(200, b'{"ok": 1}')],
    )
    s = Settings(cache_dir=tmp_path, retries=3)
    resp = _data.http_get("https://host/idx/manifest.json", s)
    assert resp.status_code == 200 and resp.content == b'{"ok": 1}'
    assert get.call_count == 3  # two transient failures, then success


def test_http_get_does_not_retry_permanent_404(mocker, tmp_path):
    """A 404 is permanent: raise at once rather than burn the retry budget."""
    import requests

    mocker.patch("time.sleep")
    get = mocker.patch("euroflood._data.requests.get", return_value=_response(404))
    s = Settings(cache_dir=tmp_path, retries=3)
    with pytest.raises(requests.HTTPError):
        _data.http_get("https://host/idx/nope.json", s)
    assert get.call_count == 1  # not retried


def test_http_get_retries_connection_error(mocker, tmp_path):
    import requests

    mocker.patch("time.sleep")
    get = mocker.patch(
        "euroflood._data.requests.get",
        side_effect=[requests.ConnectionError("reset"), _response(200)],
    )
    s = Settings(cache_dir=tmp_path, retries=3)
    assert _data.http_get("https://host/idx/m.json", s).status_code == 200
    assert get.call_count == 2


def test_http_get_gives_up_after_retries(mocker, tmp_path):
    """A persistently-500 host still raises (a transient error, surfaced)."""
    mocker.patch("time.sleep")
    get = mocker.patch("euroflood._data.requests.get", return_value=_response(500))
    s = Settings(cache_dir=tmp_path, retries=2)
    with pytest.raises(_data.TransientHTTPError):
        _data.http_get("https://host/idx/m.json", s)
    assert get.call_count == 2  # exhausted settings.retries


def test_fetch_file_retries_transient_pooch_failure(mocker, tmp_path):
    """A 5xx raised by pooch's download is retried too (the bundle tables)."""
    import requests

    mocker.patch("time.sleep")
    err = requests.HTTPError("500 Server Error")
    err.response = _response(500)
    ret = mocker.patch(
        "euroflood._data.pooch.retrieve",
        side_effect=[err, str(tmp_path / "events.parquet")],
    )
    s = Settings(cache_dir=tmp_path, retries=3)
    out = _data.fetch_file(
        "https://host/idx", "events.parquet", tmp_path, known_hash=None, settings=s
    )
    assert out == Path(tmp_path / "events.parquet")
    assert ret.call_count == 2


def test_fetch_file_does_not_retry_permanent_404(mocker, tmp_path):
    import requests

    mocker.patch("time.sleep")
    err = requests.HTTPError("404 Not Found")
    err.response = _response(404)
    ret = mocker.patch("euroflood._data.pooch.retrieve", side_effect=err)
    s = Settings(cache_dir=tmp_path, retries=3)
    with pytest.raises(requests.HTTPError):
        _data.fetch_file(
            "https://host/idx", "gone.parquet", tmp_path, known_hash=None, settings=s
        )
    assert ret.call_count == 1
