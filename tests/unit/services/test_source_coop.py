"""Tests for the Source Cooperative S3 uploader (fully mocked; no boto/network call)."""

import pytest

from euroflood.exceptions import SourceCoopError
from euroflood.services.source_coop import SourceCoopPublisher, _content_type

_CREDS = {
    "AWS_ACCESS_KEY_ID": "AKIA",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "AWS_SESSION_TOKEN": "token",
}


@pytest.fixture
def _creds(monkeypatch):
    for key, val in _CREDS.items():
        monkeypatch.setenv(key, val)


@pytest.fixture
def publisher(mocker, _creds):
    client = mocker.Mock()
    mocker.patch("euroflood.services.source_coop.boto3.client", return_value=client)
    pub = SourceCoopPublisher(endpoint="https://data.source.coop", region="us-east-1")
    return pub, client


def test_missing_credentials_raises(monkeypatch):
    for key in _CREDS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SourceCoopError, match="Missing AWS credentials"):
        SourceCoopPublisher(endpoint="https://x", region="us-east-1")


def test_client_uses_path_style_and_endpoint(mocker, _creds):
    client = mocker.patch(
        "euroflood.services.source_coop.boto3.client", return_value=mocker.Mock()
    )
    SourceCoopPublisher(endpoint="https://data.source.coop", region="us-east-1")
    kwargs = client.call_args.kwargs
    assert kwargs["endpoint_url"] == "https://data.source.coop"
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["config"].s3["addressing_style"] == "path"


def test_upload_files_keys_and_content_types(publisher, tmp_path):
    pub, client = publisher
    tif = tmp_path / "europe_flood_index.tif"
    tif.write_bytes(b"x")
    parquet = tmp_path / "events.parquet"
    parquet.write_bytes(b"y")
    keys = pub.upload_files(
        [tif, parquet], bucket="hackl", key_prefix="euroflood-index/v1.0.0"
    )
    assert keys == [
        "euroflood-index/v1.0.0/europe_flood_index.tif",
        "euroflood-index/v1.0.0/events.parquet",
    ]
    calls = client.upload_file.call_args_list
    assert calls[0].args[1] == "hackl"  # bucket
    assert calls[0].args[2] == "euroflood-index/v1.0.0/europe_flood_index.tif"
    assert calls[0].kwargs["ExtraArgs"]["ContentType"] == "image/tiff"
    assert calls[1].kwargs["ExtraArgs"]["ContentType"] == "application/octet-stream"


def test_upload_readme_targets_repo_root(publisher, tmp_path):
    pub, client = publisher
    readme = tmp_path / "README.md"
    readme.write_text("x")
    key = pub.upload_readme(readme, bucket="hackl", repository="euroflood-index")
    assert key == "euroflood-index/README.md"
    assert client.upload_file.call_args.kwargs["ExtraArgs"]["ContentType"] == (
        "text/markdown"
    )


def test_upload_error_is_wrapped(publisher, tmp_path):
    pub, client = publisher
    client.upload_file.side_effect = RuntimeError("boom")
    bad = tmp_path / "m.json"
    bad.write_text("{}")
    with pytest.raises(SourceCoopError, match="upload failed"):
        pub.upload_files([bad], bucket="b", key_prefix="p")


def test_content_type_map_and_fallback(tmp_path):
    assert _content_type(tmp_path / "x.tif") == "image/tiff"
    assert _content_type(tmp_path / "x.json") == "application/json"
    assert _content_type(tmp_path / "x.unknownext") == "application/octet-stream"
