"""Tests for the robust DownloadService."""

from concurrent.futures import ThreadPoolExecutor

import pytest
import requests

from euroflood.services.downloader import DownloadService


def test_download_success(mock_requests_get, mock_settings):
    """Test successful download writes to disk."""
    mock_requests_get.return_value.iter_content = lambda chunk_size: [b"data"]
    mock_requests_get.return_value.status_code = 200

    svc = DownloadService()
    out_path = svc.download_file("http://foo.com/a.tif", "a.tif")

    assert out_path.exists()
    assert out_path.read_bytes() == b"data"


def test_download_reports_bytes_via_callback(mock_requests_get, mock_settings):
    """on_bytes is called per chunk and sums to the total downloaded size."""
    mock_requests_get.return_value.iter_content = lambda chunk_size: [b"ab", b"cde"]
    mock_requests_get.return_value.status_code = 200

    seen: list[int] = []
    svc = DownloadService()
    out = svc.download_file("http://foo.com/a.tif", "a.tif", on_bytes=seen.append)

    assert out.read_bytes() == b"abcde"
    assert seen == [2, 3]
    assert sum(seen) == 5


def test_download_cache_hit_when_size_matches(mock_settings, mocker):
    """A cached file of the expected size is a cache hit (no re-download)."""
    svc = DownloadService()
    cached = svc.download_dir / "a.tif"
    cached.write_bytes(b"1234")  # 4 bytes
    stream = mocker.patch.object(svc, "_download_stream")

    out = svc.download_file("http://x/a.tif", "a.tif", expected_size=4)

    assert out == cached
    stream.assert_not_called()


def test_download_redownloads_on_size_mismatch(mock_settings, mocker):
    """A cached file of the wrong size is treated as corrupt and re-downloaded."""
    svc = DownloadService()
    cached = svc.download_dir / "a.tif"
    cached.write_bytes(b"12")  # 2 bytes, but we expect 4

    def fake_stream(url, path, on_bytes=None):
        path.write_bytes(b"1234")  # the corrected re-download

    stream = mocker.patch.object(svc, "_download_stream", side_effect=fake_stream)

    out = svc.download_file("http://x/a.tif", "a.tif", expected_size=4)

    stream.assert_called_once()
    assert out.read_bytes() == b"1234"


def test_download_skip_existing(mock_requests_get, mock_settings):
    """Test that existing non-empty files are skipped."""
    file_path = mock_settings.cache_dir / "downloads" / "exists.tif"
    file_path.parent.mkdir(exist_ok=True)
    file_path.write_text("content")

    svc = DownloadService()
    result = svc.download_file("http://foo.com/exists.tif", "exists.tif")

    assert result == file_path
    assert mock_requests_get.call_count == 0


def test_download_retry_and_fail(mocker, mock_settings):
    """Test that it retries and eventually returns None on failure."""
    mock_get = mocker.patch(
        "requests.get", side_effect=requests.ConnectionError("Down")
    )

    svc = DownloadService()
    res = svc.download_file("http://fail.com", "fail.tif")

    assert res is None
    assert mock_get.call_count >= 1


def test_download_cleanup_oserror(mocker, mock_settings):
    """Test cleanup logic handles OSErrors gracefully."""
    # Trigger exception in main loop
    mocker.patch("requests.get", side_effect=Exception("Fail"))

    # Create target file to trigger cleanup
    target = mock_settings.cache_dir / "downloads" / "cleanup.tif"
    target.parent.mkdir(exist_ok=True)
    target.touch()

    # Mock os.remove to raise OSError
    mocker.patch("os.remove", side_effect=OSError("Access Denied"))

    svc = DownloadService()
    # Should not raise
    svc.download_file("http://fail", "cleanup.tif")


def test_download_validates_matching_content_length(mock_requests_get, mock_settings):
    """A download whose byte count matches Content-Length is committed."""
    mock_requests_get.return_value.headers = {"Content-Length": "4"}
    mock_requests_get.return_value.iter_content = lambda chunk_size: [b"data"]

    out = DownloadService().download_file("http://h/ok.tif", "ok.tif")

    assert out is not None and out.read_bytes() == b"data"


def test_download_rejects_truncated_stream(mocker, mock_requests_get, mock_settings):
    """A short read vs Content-Length is rejected (not committed) and returns None."""
    mock_requests_get.return_value.headers = {"Content-Length": "100"}
    mock_requests_get.return_value.iter_content = lambda chunk_size: [b"data"]
    mocker.patch("time.sleep")

    out = DownloadService().download_file("http://h/short.tif", "short.tif")

    assert out is None
    assert not (mock_settings.cache_dir / "downloads" / "short.tif").exists()
    assert not (mock_settings.cache_dir / "downloads" / "short.tif.part").exists()


@pytest.mark.parametrize("retries", [1, 3])
def test_download_retries_read_settings_at_runtime(mocker, mock_settings, retries):
    """``settings.retries`` is read at call time (not frozen at import): N -> N attempts.

    The previous ``@retry`` decorator froze ``stop_after_attempt`` at import (the
    default 3), so a test override was a silent no-op; parametrising both values
    guards that a returned ``None`` and the exact attempt count both track settings.
    """
    mock_settings.retries = retries
    mocker.patch("time.sleep")  # skip tenacity backoff between attempts
    mock_get = mocker.patch(
        "requests.get", side_effect=requests.ConnectionError("down")
    )

    svc = DownloadService()
    result = svc.download_file("http://fail.example/x.tif", "x.tif")

    assert result is None
    assert mock_get.call_count == retries


def test_real_threadpool_concurrent_downloads(mock_requests_get, mock_settings):
    """DownloadService works correctly under a real ThreadPoolExecutor.

    Other tests swap in a synchronous executor; this exercises the actual
    download (thread) path: atomic temp->rename with no cross-thread collision.
    """
    mock_requests_get.return_value.iter_content = lambda chunk_size: [b"data"]

    svc = DownloadService()
    jobs = [(f"http://host/{i}.tif", f"file_{i}.tif") for i in range(8)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda job: svc.download_file(*job), jobs))

    assert all(r is not None for r in results)
    assert all(r.exists() for r in results)
    assert len({r.name for r in results}) == 8
