"""Tests for the Click CLI."""

from pathlib import Path

import geopandas as gpd
from click.testing import CliRunner
from shapely.geometry import box

from euroflood.cli import cli
from euroflood.exceptions import (
    CacheSchemaError,
    GeocodingError,
    HazardError,
    ProcessingError,
)


def _sample_catalogue():
    return gpd.GeoDataFrame(
        {
            "event_id": [10],
            "date": ["2020-05-01"],
            "area_km2": [8.0],
            "filename": ["a.tif"],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def _sample_hazard_catalogue():
    return gpd.GeoDataFrame(
        {
            "collection": ["hazard"],
            "return_period": [100],
            "n_tiles": [2],
            "area_km2": [891.2],
            "filename": ["hazard_RP100.tif"],
            "geometry": [box(0, 0, 1, 1)],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def test_cli_ingest_command(mocker):
    """Test ingest command invocation."""
    mock_run = mocker.patch("euroflood.pipelines.ingestion.IngestionPipeline.run")
    mocker.patch(
        "euroflood.pipelines.ingestion.IngestionPipeline.__init__", return_value=None
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["ingest", "--year", "2020"])

    assert result.exit_code == 0
    mock_run.assert_called_with(year=2020, month=None, update=False, limit=None)


def test_cli_export_command(mocker):
    """Test export command invocation."""
    mock_run = mocker.patch("euroflood.pipelines.export.ExportPipeline.run")
    mocker.patch(
        "euroflood.pipelines.export.ExportPipeline.__init__", return_value=None
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["export"])

    assert result.exit_code == 0
    mock_run.assert_called_once()


def _doctor_report():
    return {"populated_cells": 5, "n_combos": 2, "cog_size_bytes": 2_000_000}


def test_cli_build_index(mocker):
    """build-index runs the export then the doctor, printing a summary."""
    mocker.patch("euroflood.cli.ExportPipeline")
    doctor = mocker.patch("euroflood.cli.IndexDoctor")
    doctor.return_value.run.return_value = _doctor_report()

    result = CliRunner().invoke(cli, ["build-index"])

    assert result.exit_code == 0
    assert "Index OK" in result.output
    doctor.return_value.run.assert_called_once()


def test_cli_doctor(mocker):
    """doctor validates a built bundle and prints a summary."""
    doctor = mocker.patch("euroflood.cli.IndexDoctor")
    doctor.return_value.run.return_value = _doctor_report()

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 0
    assert "Index OK: 5 cells, 2 combos" in result.output


def test_cli_ingest_sharding(mocker):
    """ingest forwards --shard-count/--shard-index into settings."""
    mocker.patch("euroflood.pipelines.ingestion.IngestionPipeline.run")
    mocker.patch(
        "euroflood.pipelines.ingestion.IngestionPipeline.__init__", return_value=None
    )

    result = CliRunner().invoke(
        cli, ["ingest", "--year", "2020", "--shard-count", "4", "--shard-index", "1"]
    )

    assert result.exit_code == 0
    from euroflood.config import settings

    assert settings.ingest_shard_count == 4
    assert settings.ingest_shard_index == 1


def test_cli_build_hazard_manifest(mocker):
    """build-hazard-manifest authors the reference manifest."""
    mock_build = mocker.patch(
        "euroflood.cli.build_hazard_manifest", return_value=Path("hazard_manifest.json")
    )

    result = CliRunner().invoke(cli, ["build-hazard-manifest"])

    assert result.exit_code == 0
    assert "Wrote hazard manifest" in result.output
    mock_build.assert_called_once()


def test_cli_fetch_sources(mocker):
    """fetch-sources (renamed producer mirror) forwards flags and reports the count."""
    pipe = mocker.patch("euroflood.cli.MirrorPipeline")
    pipe.return_value.run.return_value = 3280

    result = CliRunner().invoke(cli, ["fetch-sources", "--verify"])

    assert result.exit_code == 0
    assert "3280 source tile(s) available" in result.output
    pipe.return_value.run.assert_called_once_with(
        year=None, update=False, verify=True, retry_failed=False, limit=None
    )


# --- dry-run (no side effects) --------------------------------------------
def test_cli_fetch_sources_dry_run(mocker):
    pipe = mocker.patch("euroflood.cli.MirrorPipeline")
    pipe.return_value.plan.return_value = {
        "tiles": 5,
        "present": 1,
        "to_download": 4,
        "total_gb": 0.04,
        "dest": "d",
    }
    result = CliRunner().invoke(cli, ["fetch-sources", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] fetch-sources" in result.output
    pipe.return_value.run.assert_not_called()


def test_cli_ingest_dry_run(mocker):
    pipe = mocker.patch("euroflood.cli.IngestionPipeline")
    pipe.return_value.plan.return_value = {
        "total": 5,
        "pending": 4,
        "done": 1,
        "dest": "d",
    }
    result = CliRunner().invoke(cli, ["ingest", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] ingest" in result.output
    pipe.return_value.run.assert_not_called()


def test_cli_build_index_dry_run(mocker):
    mocker.patch(
        "euroflood.cli.export_plan",
        return_value={
            "parquet_files": 3,
            "populated_cells": 10,
            "outputs": {"index": "x.tif"},
        },
    )
    pipe = mocker.patch("euroflood.cli.ExportPipeline")
    result = CliRunner().invoke(cli, ["build-index", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] build-index" in result.output
    pipe.return_value.run.assert_not_called()


def test_cli_download_dry_run(mocker):
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    dl = mocker.patch("euroflood.cli.api_download")
    result = CliRunner().invoke(cli, ["download", "Berlin", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] download" in result.output
    dl.assert_not_called()


def test_cli_mirror_hazard_dry_run(mocker):
    from euroflood.services.mirror_ledger import MirrorResult

    m = mocker.patch(
        "euroflood.cli.api_mirror",
        return_value=MirrorResult(
            0, n_expected=2, missing=["a.tif", "b.tif"], bytes_total=2_700_000
        ),
    )
    result = CliRunner().invoke(cli, ["mirror", "hazard", "-r", "100", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] mirror hazard" in result.output
    assert m.call_args.kwargs["dry_run"] is True  # plans, never downloads


def test_cli_floods_command(mocker):
    """floods prints a summary of the queried catalogue."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())

    result = CliRunner().invoke(cli, ["floods", "Berlin", "--year", "2020"])

    assert result.exit_code == 0
    assert "1 flood event" in result.output
    assert "a.tif" in result.output


def test_cli_download_command(mocker):
    """download searches then downloads, reporting the count."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    mock_dl = mocker.patch(
        "euroflood.cli.api_download", return_value=[Path("a.tif"), Path("b.tif")]
    )

    result = CliRunner().invoke(cli, ["download", "Berlin", "--out", "out"])

    assert result.exit_code == 0
    assert "Downloaded 2" in result.output
    mock_dl.assert_called_once()


def test_cli_download_wires_progress_callback(mocker):
    """The download command drives a progress bar by passing on_bytes to api_download."""
    sentinel = mocker.Mock()
    cm = mocker.MagicMock()
    cm.__enter__.return_value = sentinel
    mocker.patch("euroflood.cli.console.download_progress", return_value=cm)
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    dl = mocker.patch("euroflood.cli.api_download", return_value=[Path("a.tif")])

    result = CliRunner().invoke(cli, ["download", "Berlin"])

    assert result.exit_code == 0
    assert dl.call_args.kwargs["on_bytes"] is sentinel  # the bar advance is threaded in


def test_cli_floods_query_and_out(mocker, tmp_path):
    """floods applies a --query filter and writes the catalogue to -o."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    out = tmp_path / "cat.csv"

    result = CliRunner().invoke(
        cli, ["floods", "Berlin", "--query", "area_km2 > 5", "-o", str(out)]
    )

    assert result.exit_code == 0
    assert out.exists()
    assert "Wrote catalogue" in result.output


def test_cli_hazard_command(mocker):
    """hazard prints a summary of the queried return-period catalogue."""
    mock_hz = mocker.patch(
        "euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue()
    )

    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100"])

    assert result.exit_code == 0
    assert "1 hazard layer" in result.output
    assert "hazard_RP100.tif" in result.output
    mock_hz.assert_called_once_with(
        "Cologne", return_period=[100], buffer_m=0.0, shape="exact"
    )


def test_cli_hazard_multi_rp(mocker):
    """Repeating -r passes a list of return periods through."""
    mock_hz = mocker.patch(
        "euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue()
    )

    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100", "-r", "500"])

    assert result.exit_code == 0
    mock_hz.assert_called_once_with(
        "Cologne", return_period=[100, 500], buffer_m=0.0, shape="exact"
    )


def test_cli_shape_flag_forwarded(mocker):
    """--shape reaches the api call for both floods and hazard."""
    mock_fl = mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    assert (
        CliRunner().invoke(cli, ["floods", "Cologne", "--shape", "bbox"]).exit_code == 0
    )
    assert mock_fl.call_args.kwargs.get("shape") == "bbox"

    mock_hz = mocker.patch(
        "euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue()
    )
    res = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100", "--shape", "hull"])
    assert res.exit_code == 0
    assert mock_hz.call_args.kwargs.get("shape") == "hull"


def test_cli_hazard_download(mocker):
    """hazard --download fetches the maps and reports the count."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    mock_dl = mocker.patch(
        "euroflood.cli.api_download", return_value=[Path("hazard_RP100.tif")]
    )

    result = CliRunner().invoke(
        cli, ["hazard", "Cologne", "-r", "100", "--download", "--out", "out"]
    )

    assert result.exit_code == 0
    assert "Downloaded 1 hazard map" in result.output
    mock_dl.assert_called_once()


def test_cli_mirror_hazard(mocker):
    """mirror hazard forwards the ROI + RPs and reports the local count."""
    from euroflood.services.mirror_ledger import MirrorResult

    m = mocker.patch(
        "euroflood.cli.api_mirror", return_value=MirrorResult(542, n_expected=542)
    )
    result = CliRunner().invoke(
        cli, ["mirror", "hazard", "--bbox", "10.5", "50.2", "11.5", "50.8", "-r", "100"]
    )
    assert result.exit_code == 0
    assert "542 hazard tile(s) available locally" in result.output
    assert m.call_args.args[0] == "hazard"
    assert m.call_args.kwargs["bbox"] == (10.5, 50.2, 11.5, 50.8)
    assert m.call_args.kwargs["return_period"] == [100]


def test_cli_mirror_floods(mocker):
    from euroflood.services.mirror_ledger import MirrorResult

    m = mocker.patch(
        "euroflood.cli.api_mirror", return_value=MirrorResult(3, n_expected=3)
    )
    result = CliRunner().invoke(
        cli, ["mirror", "floods", "--bbox", "6.1", "52.0", "6.3", "52.2"]
    )
    assert result.exit_code == 0
    assert m.call_args.args[0] == "floods"
    assert m.call_args.kwargs["bbox"] == (6.1, 52.0, 6.3, 52.2)


def test_cli_verify_hazard_success(mocker):
    from euroflood.services.mirror_ledger import MirrorReport

    mocker.patch(
        "euroflood.cli.api_verify",
        return_value=MirrorReport(collection="hazard", present=["a"], n_expected=1),
    )
    result = CliRunner().invoke(
        cli, ["verify", "hazard", "--bbox", "6", "52", "7", "53"]
    )
    assert result.exit_code == 0
    assert "1/1 present" in result.output


def test_cli_verify_hazard_missing_exits_nonzero(mocker):
    from euroflood.services.mirror_ledger import MirrorReport

    mocker.patch(
        "euroflood.cli.api_verify",
        return_value=MirrorReport(
            collection="hazard",
            present=[],
            missing=["a.tif"],
            n_expected=1,
            remediation_cmd="euroflood mirror hazard --bbox ...",
        ),
    )
    result = CliRunner().invoke(
        cli, ["verify", "hazard", "--bbox", "6", "52", "7", "53"]
    )
    assert result.exit_code == 10  # VerificationError
    assert "Repair" in result.output


# --- _write_catalogue: parquet & geojson writer branches ------------------
def test_cli_floods_writes_parquet(mocker, tmp_path):
    """floods -o *.parquet routes through the GeoParquet writer."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    out = tmp_path / "cat.parquet"
    result = CliRunner().invoke(cli, ["floods", "Berlin", "-o", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert "Wrote catalogue" in result.output
    # Round-trip: it really is a readable GeoParquet with our event row.
    assert gpd.read_parquet(out).iloc[0]["event_id"] == 10


def test_cli_floods_writes_geojson(mocker, tmp_path):
    """floods -o *.geojson routes through the GeoJSON writer."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    out = tmp_path / "cat.geojson"
    result = CliRunner().invoke(cli, ["floods", "Berlin", "-o", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert gpd.read_file(out).iloc[0]["event_id"] == 10


# --- dry-run note lines (cli 100 / 151) -----------------------------------
def test_cli_ingest_dry_run_with_note(mocker):
    """ingest --dry-run echoes the optional sharding 'note' line."""
    pipe = mocker.patch("euroflood.cli.IngestionPipeline")
    pipe.return_value.plan.return_value = {
        "total": 5,
        "pending": 4,
        "done": 1,
        "dest": "d",
        "note": "shard 1/4 -> 2 tiles",
    }
    result = CliRunner().invoke(cli, ["ingest", "--dry-run"])
    assert result.exit_code == 0
    assert "note: shard 1/4 -> 2 tiles" in result.output
    pipe.return_value.run.assert_not_called()


def test_cli_fetch_sources_dry_run_with_note(mocker):
    """fetch-sources --dry-run echoes the optional 'note' line."""
    pipe = mocker.patch("euroflood.cli.MirrorPipeline")
    pipe.return_value.plan.return_value = {
        "tiles": 5,
        "present": 1,
        "to_download": 4,
        "total_gb": 0.04,
        "dest": "d",
        "note": "retry-failed: 2 in dead-letter ledger",
    }
    result = CliRunner().invoke(cli, ["fetch-sources", "--dry-run"])
    assert result.exit_code == 0
    assert "note: retry-failed: 2 in dead-letter ledger" in result.output
    pipe.return_value.run.assert_not_called()


# --- export / doctor dry-runs ---
def test_cli_export_dry_run(mocker):
    """export --dry-run prints the plan and runs nothing (cli lines 181-182)."""
    mocker.patch(
        "euroflood.cli.export_plan",
        return_value={
            "parquet_files": 2,
            "populated_cells": 7,
            "outputs": {"index": "idx.tif"},
        },
    )
    pipe = mocker.patch("euroflood.cli.ExportPipeline")
    result = CliRunner().invoke(cli, ["export", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] export" in result.output
    pipe.return_value.run.assert_not_called()


def test_cli_doctor_dry_run(mocker):
    """doctor --dry-run names the paths it would validate (cli lines 219-223)."""
    doctor = mocker.patch("euroflood.cli.IndexDoctor")
    result = CliRunner().invoke(cli, ["doctor", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] doctor" in result.output
    doctor.return_value.run.assert_not_called()


# --- floods -o + --dry-run ---------------------------------
def test_cli_floods_out_dry_run(mocker, tmp_path):
    """floods -o ... --dry-run prints intent but writes nothing."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    out = tmp_path / "cat.csv"
    result = CliRunner().invoke(cli, ["floods", "Berlin", "-o", str(out), "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] would write catalogue" in result.output
    assert not out.exists()


# --- download --query filter -------------------------------
def test_cli_download_query_filter(mocker):
    """download --query filters the catalogue before fetching."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    mock_dl = mocker.patch("euroflood.cli.api_download", return_value=[Path("a.tif")])
    result = CliRunner().invoke(
        cli, ["download", "Berlin", "--query", "area_km2 > 5", "--out", "out"]
    )
    assert result.exit_code == 0
    assert "Downloaded 1" in result.output
    # The filtered (still-1-row) catalogue reached the downloader.
    sent = mock_dl.call_args.args[0]
    assert len(sent) == 1


def test_cli_download_query_filters_everything_out(mocker):
    """A --query that drops all rows still fetches (0 maps) cleanly."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    mock_dl = mocker.patch("euroflood.cli.api_download", return_value=[])
    result = CliRunner().invoke(
        cli, ["download", "Berlin", "--query", "area_km2 > 1000"]
    )
    assert result.exit_code == 0
    assert "Downloaded 0" in result.output
    assert len(mock_dl.call_args.args[0]) == 0


# --- hazard --query / dry-run / -o writer (cli 409, 419-426, 431-432) -----
def test_cli_hazard_query_filter(mocker):
    """hazard --query filters the return-period catalogue."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    result = CliRunner().invoke(
        cli, ["hazard", "Cologne", "-r", "100", "--query", "return_period == 100"]
    )
    assert result.exit_code == 0
    assert "1 hazard layer" in result.output


def test_cli_hazard_download_dry_run(mocker):
    """hazard --download --dry-run prints the fetch plan only (cli lines 419-423)."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    dl = mocker.patch("euroflood.cli.api_download")
    result = CliRunner().invoke(
        cli, ["hazard", "Cologne", "-r", "100", "--download", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "[dry-run] hazard: would fetch + mosaic" in result.output
    dl.assert_not_called()


def test_cli_hazard_out_dry_run(mocker, tmp_path):
    """hazard -o ... --dry-run prints intent; writes nothing (cli lines 424-425)."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    out = tmp_path / "haz.csv"
    result = CliRunner().invoke(
        cli, ["hazard", "Cologne", "-r", "100", "-o", str(out), "--dry-run"]
    )
    assert result.exit_code == 0
    assert "[dry-run] would write catalogue" in result.output
    assert not out.exists()


def test_cli_hazard_writes_catalogue(mocker, tmp_path):
    """hazard -o (no --download) writes the catalogue file (cli lines 431-432)."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    out = tmp_path / "haz.csv"
    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100", "-o", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert "Wrote catalogue" in result.output


def test_cli_hazard_writes_geojson(mocker, tmp_path):
    """hazard -o *.geojson exercises the GeoJSON writer end-to-end."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    out = tmp_path / "haz.geojson"
    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100", "-o", str(out)])
    assert result.exit_code == 0
    assert gpd.read_file(out).iloc[0]["return_period"] == 100


# --- empty-catalogue branches ----------
def _empty_catalogue():
    return gpd.GeoDataFrame(
        {
            "event_id": [],
            "date": [],
            "area_km2": [],
            "filename": [],
            "geometry": [],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def _empty_hazard_catalogue():
    return gpd.GeoDataFrame(
        {
            "collection": [],
            "return_period": [],
            "n_tiles": [],
            "area_km2": [],
            "filename": [],
            "geometry": [],
        },
        geometry="geometry",
        crs="EPSG:4326",
    )


def test_cli_floods_empty_skips_table(mocker):
    """floods with no matches reports 0 and skips the table."""
    mocker.patch("euroflood.cli.api_floods", return_value=_empty_catalogue())
    result = CliRunner().invoke(cli, ["floods", "Nowhere"])
    assert result.exit_code == 0
    assert "0 flood event(s)" in result.output


def test_cli_hazard_empty_skips_table(mocker):
    """hazard with no matches reports 0 and skips the table."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_empty_hazard_catalogue())
    result = CliRunner().invoke(cli, ["hazard", "Nowhere", "-r", "100"])
    assert result.exit_code == 0
    assert "0 hazard layer(s)" in result.output


def test_cli_hazard_dry_run_no_targets(mocker):
    """hazard --dry-run with neither --download nor -o just returns."""
    mocker.patch("euroflood.cli.api_hazard", return_value=_sample_hazard_catalogue())
    dl = mocker.patch("euroflood.cli.api_download")
    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run]" not in result.output  # no fetch/write plan line emitted
    dl.assert_not_called()


# --- build-hazard-manifest --dry-run (cli lines 467-471) ------------------
def test_cli_build_hazard_manifest_dry_run(mocker):
    """build-hazard-manifest --dry-run names the target and writes nothing."""
    mock_build = mocker.patch("euroflood.cli.build_hazard_manifest")
    result = CliRunner().invoke(cli, ["build-hazard-manifest", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] build-hazard-manifest" in result.output
    mock_build.assert_not_called()


# --- publish (Zenodo) -----------------------------------------------------
def test_cli_publish_dry_run(mocker):
    """publish --dry-run prints the plan and uploads nothing."""
    mocker.patch(
        "euroflood.pipelines.publish.publish_to_zenodo",
        return_value={
            "dry_run": True,
            "version": "v1.0.0",
            "target": "sandbox",
            "token_env": "ZENODO_SANDBOX_TOKEN",
            "files": ["manifest.json", "README.md"],
        },
    )
    result = CliRunner().invoke(
        cli, ["publish", "--zenodo", "--version", "v1.0.0", "--sandbox", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "[dry-run] publish v1.0.0 -> Zenodo sandbox" in result.output


def test_cli_publish_reports_doi(mocker):
    """A real publish prints the minted DOI + record URL."""
    mocker.patch(
        "euroflood.pipelines.publish.publish_to_zenodo",
        return_value={
            "doi": "10.5281/zenodo.9",
            "concept_doi": "10.5281/zenodo.8",
            "record_url": "https://zenodo.org/record/9",
        },
    )
    result = CliRunner().invoke(cli, ["publish", "--zenodo", "--version", "v1.0.0"])
    assert result.exit_code == 0
    assert "Published v1.0.0 to Zenodo: DOI 10.5281/zenodo.9" in result.output


# --- error seam (clean messages, no tracebacks, per-type exit codes) -------
def test_cli_geocoding_error_is_clean(mocker):
    """A GeocodingError renders a clean one-line error (exit 4, no traceback)."""
    mocker.patch(
        "euroflood.cli.api_floods",
        side_effect=GeocodingError("No match for 'Xyz'. Did you mean: cologne?"),
    )
    result = CliRunner().invoke(cli, ["floods", "Xyz"])
    assert result.exit_code == 4
    assert "Did you mean: cologne?" in result.output
    assert "Traceback" not in result.output


def test_cli_cache_schema_error_exit_code(mocker):
    """A CacheSchemaError maps to exit 7 and suggests a rebuild."""
    doctor = mocker.patch("euroflood.cli.IndexDoctor")
    doctor.return_value.run.side_effect = CacheSchemaError("Index COG is missing.")
    result = CliRunner().invoke(cli, ["doctor"])
    assert result.exit_code == 7
    assert "Index COG is missing." in result.output
    assert "build-index" in result.output  # the next-step hint


def test_cli_file_not_found_is_clean(mocker):
    """A missing index (FileNotFoundError) is caught (exit 2), not a traceback."""
    mocker.patch(
        "euroflood.cli.api_floods",
        side_effect=FileNotFoundError("Index raster not found."),
    )
    result = CliRunner().invoke(cli, ["floods", "Berlin"])
    assert result.exit_code == 2
    assert "Index raster not found." in result.output
    assert "Traceback" not in result.output


def test_cli_hazard_error_exit_code(mocker):
    """A HazardError maps to exit 6."""
    mocker.patch("euroflood.cli.api_hazard", side_effect=HazardError("no tiles"))
    result = CliRunner().invoke(cli, ["hazard", "Cologne", "-r", "100"])
    assert result.exit_code == 6


def test_cli_verbose_reraises_for_traceback(mocker):
    """With -v, the domain error propagates (developer traceback), not swallowed."""
    mocker.patch("euroflood.cli.api_floods", side_effect=GeocodingError("boom"))
    result = CliRunner().invoke(cli, ["-v", "floods", "Xyz"])
    assert result.exit_code == 1
    assert isinstance(result.exception, GeocodingError)


def test_cli_unexpected_exception_stays_loud(mocker):
    """A non-EuroFlood exception is NOT caught by the seam (propagates)."""
    mocker.patch("euroflood.cli.api_floods", side_effect=RuntimeError("bug"))
    result = CliRunner().invoke(cli, ["floods", "Berlin"])
    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_cli_quiet_suppresses_but_shows_result(mocker):
    """--quiet keeps the primary result table but drops secondary status."""
    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    result = CliRunner().invoke(cli, ["-q", "floods", "Berlin"])
    assert result.exit_code == 0
    assert "a.tif" in result.output


def test_cli_json_output_mode(mocker):
    """--json emits machine-readable records instead of a table."""
    import json

    mocker.patch("euroflood.cli.api_floods", return_value=_sample_catalogue())
    result = CliRunner().invoke(cli, ["--json", "floods", "Berlin"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data[0]["event_id"] == 10


def test_cli_mirror_index_dry_run(mocker):
    """mirror index --dry-run prints the plan and mirrors nothing."""
    from euroflood.services.mirror_ledger import MirrorResult

    m = mocker.patch(
        "euroflood.cli.api_mirror",
        return_value=MirrorResult(0, n_expected=5, missing=["a", "b", "c", "d", "e"]),
    )
    result = CliRunner().invoke(cli, ["mirror", "index", "--dry-run"])
    assert result.exit_code == 0
    assert "[dry-run] mirror index" in result.output
    assert m.call_args.kwargs["dry_run"] is True


def test_cli_mirror_index_runs(mocker):
    """mirror index pulls the full bundle and confirms."""
    from euroflood.services.mirror_ledger import MirrorResult

    m = mocker.patch(
        "euroflood.cli.api_mirror", return_value=MirrorResult(5, n_expected=5)
    )
    result = CliRunner().invoke(cli, ["mirror", "index"])
    assert result.exit_code == 0
    assert "Index mirrored" in result.output
    assert m.call_args.args[0] == "index"


def test_exit_code_and_next_step_fallbacks():
    """The defensive fallbacks: unknown exc -> exit 1 / no next-step hint."""
    from euroflood.cli import _exit_code_for, _next_step_for

    assert _exit_code_for(ValueError("x")) == 1
    assert _next_step_for(ProcessingError("x")) is None


def test_cli_publish_source_coop_dry_run(built_index):
    result = CliRunner().invoke(
        cli, ["publish", "--source-coop", "--version", "1.0.0", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "Source Cooperative" in result.output


def test_cli_verify_remote_success(mocker):
    from euroflood.pipelines.verify import VerifyReport

    report = VerifyReport(base_url="https://x/y/v1")
    report.record("manifest_fetched", True, "4 files")
    mocker.patch("euroflood.pipelines.verify.verify_published", return_value=report)
    result = CliRunner().invoke(cli, ["verify", "remote", "--url", "https://x/y/v1"])
    assert result.exit_code == 0, result.output
    assert "checks passed" in result.output


def test_cli_verify_remote_failure_exits_10(mocker):
    from euroflood.pipelines.verify import VerifyReport

    report = VerifyReport(base_url="https://x/y/v1")
    report.record("manifest_fetched", False, "boom")
    mocker.patch("euroflood.pipelines.verify.verify_published", return_value=report)
    result = CliRunner().invoke(cli, ["verify", "remote", "--url", "https://x/y/v1"])
    assert result.exit_code == 10


def test_cli_verify_remote_no_url_errors(mocker):
    mocker.patch("euroflood._data.DEFAULT_INDEX_BASE_URL", None)
    result = CliRunner().invoke(cli, ["verify", "remote"])
    assert result.exit_code != 0
    assert "No index base URL" in result.output


def test_cli_publish_zenodo_dry_run_reports_new_version_mode(mocker):
    """--record-id surfaces that an existing record gets a new version, not a new record."""
    mocker.patch(
        "euroflood.pipelines.publish.publish_to_zenodo",
        return_value={
            "dry_run": True,
            "version": "1.1.0",
            "target": "production",
            "token_env": "ZENODO_TOKEN",
            "files": ["manifest.json", "README.md"],
            "mode": "new-version",
            "record_id": 21284460,
        },
    )
    result = CliRunner().invoke(
        cli,
        [
            "publish",
            "--zenodo",
            "--version",
            "1.1.0",
            "--record-id",
            "21284460",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "[new-version of record 21284460]" in result.output


def test_cli_publish_zenodo_passes_record_id_through(mocker):
    """The CLI hands --record-id to the pipeline so the concept DOI is preserved."""
    spy = mocker.patch(
        "euroflood.pipelines.publish.publish_to_zenodo",
        return_value={
            "doi": "10.5281/zenodo.9",
            "concept_doi": "10.5281/zenodo.8",
            "record_url": "https://zenodo.org/record/9",
        },
    )
    result = CliRunner().invoke(
        cli, ["publish", "--zenodo", "--version", "1.1.0", "--record-id", "21284460"]
    )
    assert result.exit_code == 0
    assert spy.call_args.kwargs["record_id"] == 21284460
