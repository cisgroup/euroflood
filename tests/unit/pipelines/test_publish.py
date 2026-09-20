"""Tests for the publish orchestration (Zenodo), fully mocked."""

import json
from pathlib import Path

import pytest

from euroflood.pipelines import publish as pub


def test_load_dotenv_sets_without_overriding(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nexport A=aaa\nB='bbb'\nZENODO_TOKEN=tok\n\n")
    monkeypatch.setenv("A", "already")  # pre-existing must win
    monkeypatch.delenv("B", raising=False)
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)

    pub.load_dotenv(env)
    import os

    assert os.environ["A"] == "already"  # not overridden
    assert os.environ["B"] == "bbb"
    assert os.environ["ZENODO_TOKEN"] == "tok"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    pub.load_dotenv(tmp_path / "nope.env")  # must not raise


def test_build_zenodo_metadata_shape():
    m = pub.build_zenodo_metadata("v1.2.3")
    assert m["license"] == "cc-by-4.0"
    assert m["version"] == "v1.2.3"
    assert m["creators"][0]["orcid"] == "0000-0002-8849-5751"
    assert any(r["identifier"] == pub.JRC_SOURCE_URL for r in m["related_identifiers"])


def test_gather_bundle_files_from_manifest(built_index):
    files = pub.gather_bundle_files(built_index.settings)
    names = {f.name for f in files}
    assert "manifest.json" in names
    assert "europe_flood_index.tif" in names
    assert "flood_dictionary.parquet" in names


def test_publish_without_token_raises(built_index, monkeypatch, tmp_path):
    """A real (non-dry-run) publish refuses to proceed without the token env var."""
    monkeypatch.delenv("ZENODO_SANDBOX_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="ZENODO_SANDBOX_TOKEN is not set"):
        # Point dotenv at a nonexistent file so a real .env can't re-supply the token.
        pub.publish_to_zenodo(
            built_index.settings,
            version="v9.9.9",
            sandbox=True,
            dotenv=tmp_path / "absent.env",
        )


def test_publish_dry_run_is_read_only(built_index):
    """Dry-run validates + lists files but uploads/mutates nothing."""
    manifest_before = built_index.settings.get_manifest_path().read_text()
    out = pub.publish_to_zenodo(
        built_index.settings, version="v9.9.9", sandbox=True, dry_run=True
    )
    assert out["dry_run"] is True
    assert out["target"] == "sandbox"
    assert out["token_env"] == "ZENODO_SANDBOX_TOKEN"
    assert "README.md" in out["files"]
    # No mutation.
    assert built_index.settings.get_manifest_path().read_text() == manifest_before
    assert not (built_index.settings.cache_dir / "README.md").exists()


def test_publish_stamps_manifest_with_doi(built_index, mocker, monkeypatch):
    """A real publish mints a DOI and stamps it (+ version) into the manifest."""
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", "tok")
    fake = mocker.patch("euroflood.pipelines.publish.ZenodoPublisher")
    fake.return_value.create_and_publish.return_value = {
        "doi": "10.5072/zenodo.42",
        "concept_doi": "10.5072/zenodo.41",
        "record_url": "https://sandbox.zenodo.org/record/42",
        "deposition_id": 42,
    }
    result = pub.publish_to_zenodo(
        built_index.settings,
        version="v1.0.0",
        sandbox=True,
        dotenv=built_index.settings.cache_dir / "none",
    )
    assert result["doi"] == "10.5072/zenodo.42"
    # README written + manifest stamped with version + DOI.
    assert (built_index.settings.cache_dir / "README.md").exists()
    manifest = json.loads(built_index.settings.get_manifest_path().read_text())
    assert manifest["index_version"] == "v1.0.0"
    assert manifest["source_urls"]["zenodo_doi"] == "10.5072/zenodo.42"
    # The client was pointed at sandbox with the sandbox token.
    fake.assert_called_once_with("tok", sandbox=True)


def test_zenodo_publish_preserves_existing_source_coop_url(
    built_index, mocker, monkeypatch
):
    """Publishing to Zenodo must not wipe an existing source_coop URL in the manifest."""
    from euroflood.core.manifest import stamp_manifest

    mp = built_index.settings.get_manifest_path()
    stamp_manifest(
        mp, source_urls={"source_coop": "https://data.source.coop/x/y/v1.0.0"}
    )
    monkeypatch.setenv("ZENODO_SANDBOX_TOKEN", "tok")
    fake = mocker.patch("euroflood.pipelines.publish.ZenodoPublisher")
    fake.return_value.create_and_publish.return_value = {
        "doi": "10.5072/zenodo.42",
        "concept_doi": "10.5072/zenodo.41",
        "record_url": "https://sandbox.zenodo.org/record/42",
        "deposition_id": 42,
    }
    pub.publish_to_zenodo(
        built_index.settings,
        version="v1.0.0",
        sandbox=True,
        dotenv=built_index.settings.cache_dir / "none",
    )
    urls = json.loads(mp.read_text())["source_urls"]
    assert urls["source_coop"] == "https://data.source.coop/x/y/v1.0.0"  # preserved
    assert urls["zenodo_doi"] == "10.5072/zenodo.42"  # + new DOI merged in


# --- Source Cooperative publishing ------------------------------------------
def test_build_readme_fills_placeholders():
    base = "https://data.source.coop/hackl/euroflood-index/v1.0.0"
    md = pub.build_readme("1.0.0", base_url=base)
    assert "__VERSION__" not in md and "__BASE_URL__" not in md
    assert "__ZENODO_DOI_LINE__" not in md  # placeholder always substituted
    assert "v1.0.0/" in md
    assert base in md
    # the product-card hero references the public, version-pinned URL
    assert f"{base}/hero.png" in md
    assert "CC-BY-4.0" in md
    # No DOI passed -> no cite line.
    assert "doi.org" not in md


def test_build_readme_includes_zenodo_doi_when_given():
    base = "https://data.source.coop/hackl/euroflood-index/v1.0.0"
    md = pub.build_readme(
        "1.0.0", base_url=base, zenodo_concept_doi="10.5281/zenodo.42"
    )
    assert "__ZENODO_DOI_LINE__" not in md
    assert "https://doi.org/10.5281/zenodo.42" in md
    assert "Cite" in md


def test_source_coop_dry_run_is_read_only(built_index):
    before = built_index.settings.get_manifest_path().read_text()
    out = pub.publish_to_source_coop(
        built_index.settings, version="1.0.0", dry_run=True
    )
    assert out["dry_run"] is True
    assert out["base_url"].endswith("/euroflood-index/v1.0.0")
    assert "README.md" in out["files"]
    assert "hero.png" in out["files"]
    # No mutation.
    assert built_index.settings.get_manifest_path().read_text() == before
    assert not (built_index.settings.cache_dir / "README.md").exists()
    assert not (built_index.settings.cache_dir / "hero.png").exists()


def test_publish_source_coop_uploads_and_verifies(built_index, mocker, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "x")
    publisher = mocker.patch("euroflood.services.source_coop.SourceCoopPublisher")
    publisher.return_value.upload_files.return_value = ["euroflood-index/v1.0.0/m.json"]
    publisher.return_value.upload_readme.return_value = "euroflood-index/README.md"
    report = mocker.Mock(ok=True)
    report.summary.return_value = "14/14 checks passed"
    verify = mocker.patch(
        "euroflood.pipelines.verify.verify_published", return_value=report
    )

    result = pub.publish_to_source_coop(
        built_index.settings,
        version="1.0.0",
        dotenv=built_index.settings.cache_dir / "none",
    )

    assert result["verified"] is True
    assert result["base_url"].endswith("/euroflood-index/v1.0.0")
    # README + hero staged, manifest stamped with the version + the public URL.
    assert (built_index.settings.cache_dir / "README.md").exists()
    assert (built_index.settings.cache_dir / "hero.png").exists()
    manifest = json.loads(built_index.settings.get_manifest_path().read_text())
    assert manifest["index_version"] == "1.0.0"
    assert manifest["source_urls"]["source_coop"].endswith("/euroflood-index/v1.0.0")
    publisher.return_value.upload_files.assert_called_once()
    # The hero is uploaded alongside the bundle (into the version prefix).
    uploaded = publisher.return_value.upload_files.call_args.args[0]
    assert any(p.name == "hero.png" for p in uploaded)
    publisher.return_value.upload_readme.assert_called_once()
    verify.assert_called_once()


def test_publish_source_coop_can_skip_verify(built_index, mocker, monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "x")
    mocker.patch("euroflood.services.source_coop.SourceCoopPublisher")
    verify = mocker.patch("euroflood.pipelines.verify.verify_published")
    result = pub.publish_to_source_coop(
        built_index.settings,
        version="1.0.0",
        verify=False,
        dotenv=built_index.settings.cache_dir / "none",
    )
    assert result["verified"] is None
    verify.assert_not_called()


def test_source_coop_readme_only_leaves_the_version_prefix_untouched(
    built_index, mocker, monkeypatch
):
    """The card is mutable; the vX.Y.Z/ prefix is not. --readme-only must not rewrite it."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "x")
    publisher = mocker.patch("euroflood.services.source_coop.SourceCoopPublisher")
    publisher.return_value.upload_readme.return_value = "euroflood-index/README.md"
    verify = mocker.patch("euroflood.pipelines.verify.verify_published")
    manifest_path = built_index.settings.get_manifest_path()
    before = manifest_path.read_text()

    result = pub.publish_to_source_coop(
        built_index.settings,
        version="1.0.0",
        readme_only=True,
        dotenv=built_index.settings.cache_dir / "none",
    )

    assert result["readme_only"] is True
    assert result["keys"] == []
    assert result["readme_key"] == "euroflood-index/README.md"
    # Only the root card goes up; nothing is written under the version prefix.
    publisher.return_value.upload_files.assert_not_called()
    publisher.return_value.upload_readme.assert_called_once()
    # The published manifest must keep the exact bytes it was published with.
    assert manifest_path.read_text() == before
    # No hero is staged, and re-verifying an untouched bundle is pointless.
    assert not (built_index.settings.cache_dir / "hero.png").exists()
    verify.assert_not_called()


def test_source_coop_readme_only_renders_the_zenodo_doi_into_the_card(
    built_index, mocker, monkeypatch
):
    """The point of the flag: a DOI minted after upload reaches the card."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "x")
    mocker.patch("euroflood.services.source_coop.SourceCoopPublisher")
    pub.stamp_manifest(
        built_index.settings.get_manifest_path(),
        source_urls={"zenodo_concept_doi": "10.5281/zenodo.21284459"},
    )

    pub.publish_to_source_coop(
        built_index.settings,
        version="1.0.0",
        readme_only=True,
        dotenv=built_index.settings.cache_dir / "none",
    )

    card = (built_index.settings.cache_dir / "README.md").read_text()
    assert "https://doi.org/10.5281/zenodo.21284459" in card


def test_source_coop_readme_only_dry_run_lists_just_the_card(built_index):
    out = pub.publish_to_source_coop(
        built_index.settings, version="1.0.0", readme_only=True, dry_run=True
    )
    assert out["files"] == ["README.md"]
    assert out["readme_only"] is True
    assert not (built_index.settings.cache_dir / "README.md").exists()


# --- software archive (separate from the index dataset path) ----------------
def test_build_software_metadata_is_software_not_dataset():
    """The two builders describe different artifacts and must not converge."""
    sw = pub.build_software_metadata("0.3.0")
    ds = pub.build_zenodo_metadata("1.1.0")
    assert sw["upload_type"] == "software" and ds["upload_type"] == "dataset"
    assert sw["license"] == "mit" and ds["license"] == "cc-by-4.0"
    assert sw["version"] == "0.3.0"
    # The software record points at the index dataset, not the other way around.
    rels = {r["relation"]: r["identifier"] for r in sw["related_identifiers"]}
    assert rels["isSupplementedBy"] == pub.INDEX_CONCEPT_DOI


def test_software_metadata_orcid_matches_citation_cff():
    """Guards the one field that could silently drift from the citation record."""
    cff = (Path(__file__).resolve().parents[3] / "CITATION.cff").read_text()
    orcid = pub.build_software_metadata("0.3.0")["creators"][0]["orcid"]
    assert orcid in cff


def test_publish_software_requires_exactly_one_target(tmp_path):
    f = tmp_path / "euroflood-0.3.0.tar.gz"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="exactly one"):
        pub.publish_software_to_zenodo([f], version="0.3.0")
    with pytest.raises(ValueError, match="exactly one"):
        pub.publish_software_to_zenodo(
            [f], version="0.3.0", draft_id=1, concept_recid=2
        )


def test_publish_software_rejects_a_missing_artifact(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        pub.publish_software_to_zenodo(
            [tmp_path / "nope.tar.gz"], version="0.3.0", draft_id=1
        )


def test_publish_software_dry_run_uploads_nothing(tmp_path):
    f = tmp_path / "euroflood-0.3.0.tar.gz"
    f.write_bytes(b"x")
    out = pub.publish_software_to_zenodo(
        [f], version="0.3.0", draft_id=77, dry_run=True, dotenv=tmp_path / "none"
    )
    assert out["dry_run"] is True
    assert out["mode"] == "reserved-draft"
    assert out["files"] == ["euroflood-0.3.0.tar.gz"]


def test_publish_software_first_release_uses_the_reserved_draft(
    tmp_path, mocker, monkeypatch
):
    monkeypatch.setenv("ZENODO_TOKEN", "tok")
    f = tmp_path / "euroflood-0.3.0.tar.gz"
    f.write_bytes(b"x")
    cls = mocker.patch("euroflood.pipelines.publish.ZenodoPublisher")
    cls.return_value.publish_reserved_draft.return_value = {
        "doi": "10.5281/zenodo.22837459",
        "concept_doi": "10.5281/zenodo.22837458",
        "record_url": "https://zenodo.org/record/22837459",
        "deposition_id": 22837459,
    }
    out = pub.publish_software_to_zenodo(
        [f], version="0.3.0", draft_id=22837459, dotenv=tmp_path / "none"
    )
    assert out["concept_doi"] == "10.5281/zenodo.22837458"
    cls.return_value.publish_reserved_draft.assert_called_once()
    cls.return_value.publish_new_version.assert_not_called()


def test_publish_software_later_release_resolves_the_concept_record(
    tmp_path, mocker, monkeypatch
):
    monkeypatch.setenv("ZENODO_TOKEN", "tok")
    f = tmp_path / "euroflood-0.4.0.tar.gz"
    f.write_bytes(b"x")
    cls = mocker.patch("euroflood.pipelines.publish.ZenodoPublisher")
    cls.return_value.latest_version_id.return_value = 22837459
    cls.return_value.publish_new_version.return_value = {
        "doi": "10.5281/zenodo.99",
        "concept_doi": "10.5281/zenodo.22837458",
        "record_url": "https://zenodo.org/record/99",
        "deposition_id": 99,
    }
    pub.publish_software_to_zenodo(
        [f], version="0.4.0", concept_recid=22837458, dotenv=tmp_path / "none"
    )
    # It must discover the latest version rather than hard-code a record id.
    cls.return_value.latest_version_id.assert_called_once_with(22837458)
    assert cls.return_value.publish_new_version.call_args.args[0] == 22837459


def test_publish_software_never_touches_the_index_manifest(
    built_index, tmp_path, mocker, monkeypatch
):
    """Stamping a software DOI into the index manifest would corrupt its provenance."""
    monkeypatch.setenv("ZENODO_TOKEN", "tok")
    f = tmp_path / "euroflood-0.3.0.tar.gz"
    f.write_bytes(b"x")
    manifest = built_index.settings.get_manifest_path()
    before = manifest.read_text()
    cls = mocker.patch("euroflood.pipelines.publish.ZenodoPublisher")
    cls.return_value.publish_reserved_draft.return_value = {
        "doi": "d",
        "concept_doi": "c",
        "record_url": "u",
        "deposition_id": 1,
    }
    pub.publish_software_to_zenodo(
        [f], version="0.3.0", draft_id=1, dotenv=tmp_path / "none"
    )
    assert manifest.read_text() == before
