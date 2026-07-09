"""Tests for the Zenodo REST client (fully mocked; no network)."""

import pytest

from euroflood.services.zenodo import ZenodoError, ZenodoPublisher


def _resp(mocker, *, ok=True, status=200, json_data=None, text=""):
    r = mocker.Mock()
    r.ok = ok
    r.status_code = status
    r.text = text
    r.json = mocker.Mock(return_value=json_data or {})
    return r


def test_base_url_sandbox_vs_prod():
    assert ZenodoPublisher("t").base == "https://zenodo.org/api"
    assert ZenodoPublisher("t", sandbox=True).base == "https://sandbox.zenodo.org/api"


def test_create_and_publish_flow(mocker, tmp_path):
    f = tmp_path / "events.parquet"
    f.write_bytes(b"data")
    base = "https://sandbox.zenodo.org/api"
    responses = {
        ("POST", f"{base}/deposit/depositions"): _resp(
            mocker, json_data={"id": 7, "links": {"bucket": "https://bkt"}}
        ),
        ("PUT", "https://bkt/events.parquet"): _resp(mocker),
        ("PUT", f"{base}/deposit/depositions/7"): _resp(mocker),
        ("POST", f"{base}/deposit/depositions/7/actions/publish"): _resp(
            mocker,
            json_data={
                "doi": "10.5072/zenodo.9",
                "conceptdoi": "10.5072/zenodo.8",
                "links": {"record_html": "https://rec/9"},
            },
        ),
    }
    mocker.patch(
        "requests.Session.request",
        side_effect=lambda method, url, **kw: responses[(method, url)],
    )
    out = ZenodoPublisher("tok", sandbox=True).create_and_publish([f], {"title": "x"})
    assert out["doi"] == "10.5072/zenodo.9"
    assert out["concept_doi"] == "10.5072/zenodo.8"
    assert out["record_url"] == "https://rec/9"
    assert out["deposition_id"] == 7


def test_non_2xx_raises_zenodo_error(mocker):
    mocker.patch(
        "requests.Session.request",
        return_value=_resp(mocker, ok=False, status=400, text="bad metadata"),
    )
    with pytest.raises(ZenodoError, match=r"400.*bad metadata"):
        ZenodoPublisher("t", sandbox=True).create_deposition()
