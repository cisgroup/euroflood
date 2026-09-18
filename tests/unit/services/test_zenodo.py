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


def _new_version_responses(mocker, base, *, inherited=("old-a", "old-b")):
    """Wire the full new-version flow: newversion -> draft -> clear -> upload -> publish."""
    responses = {
        ("POST", f"{base}/deposit/depositions/42/actions/newversion"): _resp(
            mocker,
            json_data={"links": {"latest_draft": f"{base}/deposit/depositions/11"}},
        ),
        ("GET", f"{base}/deposit/depositions/11"): _resp(
            mocker, json_data={"id": 11, "links": {"bucket": "https://bkt2"}}
        ),
        ("GET", f"{base}/deposit/depositions/11/files"): _resp(
            mocker, json_data=[{"id": fid} for fid in inherited]
        ),
        ("PUT", "https://bkt2/events.parquet"): _resp(mocker),
        ("PUT", f"{base}/deposit/depositions/11"): _resp(mocker),
        ("POST", f"{base}/deposit/depositions/11/actions/publish"): _resp(
            mocker,
            json_data={
                "doi": "10.5072/zenodo.99",
                "conceptdoi": "10.5072/zenodo.8",
                "links": {"record_html": "https://rec/99"},
            },
        ),
    }
    for fid in inherited:
        responses[("DELETE", f"{base}/deposit/depositions/11/files/{fid}")] = _resp(
            mocker
        )
    return responses


def test_publish_new_version_keeps_concept_doi_and_mints_a_new_version_doi(
    mocker, tmp_path
):
    f = tmp_path / "events.parquet"
    f.write_bytes(b"data")
    base = "https://sandbox.zenodo.org/api"
    responses = _new_version_responses(mocker, base)
    calls = []

    def _record(method, url, **kw):
        calls.append((method, url))
        return responses[(method, url)]

    mocker.patch("requests.Session.request", side_effect=_record)
    out = ZenodoPublisher("tok", sandbox=True).publish_new_version(
        42, [f], {"title": "x", "version": "1.1.0"}
    )

    # The concept DOI is the whole point: it must survive across versions.
    assert out["concept_doi"] == "10.5072/zenodo.8"
    assert out["doi"] == "10.5072/zenodo.99"
    assert out["deposition_id"] == 11
    # It must go through the newversion action, never create a standalone deposition.
    assert ("POST", f"{base}/deposit/depositions/42/actions/newversion") in calls
    assert ("POST", f"{base}/deposit/depositions") not in calls


def test_publish_new_version_deletes_inherited_files_before_uploading(mocker, tmp_path):
    """A new-version draft copies the old bundle forward; it must go first."""
    f = tmp_path / "events.parquet"
    f.write_bytes(b"data")
    base = "https://sandbox.zenodo.org/api"
    responses = _new_version_responses(mocker, base)
    calls = []

    def _record(method, url, **kw):
        calls.append((method, url))
        return responses[(method, url)]

    mocker.patch("requests.Session.request", side_effect=_record)
    ZenodoPublisher("tok", sandbox=True).publish_new_version(42, [f], {"title": "x"})

    deletes = [i for i, (m, _) in enumerate(calls) if m == "DELETE"]
    uploads = [i for i, (m, u) in enumerate(calls) if m == "PUT" and "bkt2" in u]
    assert len(deletes) == 2, "both inherited files should be removed"
    assert max(deletes) < min(uploads), "stale files must be deleted before upload"


def test_new_version_without_a_draft_link_raises(mocker):
    mocker.patch(
        "requests.Session.request",
        side_effect=lambda method, url, **kw: _resp(mocker, json_data={"links": {}}),
    )
    with pytest.raises(ZenodoError, match=r"no links\.latest_draft"):
        ZenodoPublisher("t", sandbox=True).new_version(42)


def test_clear_files_is_a_noop_when_the_draft_has_none(mocker):
    base = "https://sandbox.zenodo.org/api"
    responses = {
        ("GET", f"{base}/deposit/depositions/11/files"): _resp(mocker, json_data=[])
    }
    mocker.patch(
        "requests.Session.request",
        side_effect=lambda method, url, **kw: responses[(method, url)],
    )
    assert ZenodoPublisher("t", sandbox=True).clear_files(11) == 0
