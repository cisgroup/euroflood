"""Tests for the JRC Website Scraper."""

import pytest
from bs4 import BeautifulSoup
from requests import RequestException

from euroflood.exceptions import ScrapingError
from euroflood.services.scraper import ScraperService, _parse_size, _stable_global_id


def test_parse_size_units():
    """Apache-style size cells parse to bytes; non-size cells return None."""
    assert _parse_size("452M") == 452 * 1024**2
    assert _parse_size("120K") == 120 * 1024
    assert _parse_size("1.2G") == int(1.2 * 1024**3)
    assert _parse_size("2024-01-01 12:00") is None  # a date cell
    assert _parse_size("WD_MERGE_x.tif") is None  # a filename cell
    assert _parse_size("-") is None  # a directory row
    assert _parse_size("") is None


def test_fetch_all_records_parses_size(mock_requests_get, mocker):
    """The file Size from the listing table is captured into size_bytes."""
    table = (
        "<table><tr>"
        "<td><img></td>"
        '<td><a href="WD_MERGE_2020-01-01---2020-01-05_cluster_10.tif">f</a></td>'
        '<td align="right">2024-01-01 12:00</td>'
        '<td align="right">10.5M</td>'
        "</tr></table>"
    )
    mock_requests_get.side_effect = [
        mocker.Mock(text='<a href="2020/">2020</a>', status_code=200),
        mocker.Mock(text=table, status_code=200),
    ]

    records = ScraperService().fetch_all_records()
    assert records[0]["size_bytes"] == int(10.5 * 1024**2)


def test_stable_global_id_is_deterministic():
    """global_id is a stable, uint32-fitting hash of the filename."""
    a = _stable_global_id("WD_MERGE_2020_x.tif")
    assert a == _stable_global_id("WD_MERGE_2020_x.tif")
    assert 0 <= a <= 2**32 - 1
    assert a != _stable_global_id("WD_MERGE_2020_y.tif")


def test_scraper_get_year_links(mock_requests_get):
    """Test parsing of year directories."""
    mock_requests_get.return_value.text = """
    <html>
        <a href="2018/">2018</a>
        <a href="2019/">2019</a>
        <a href="readme.txt">readme</a>
    </html>
    """

    scraper = ScraperService()
    years = scraper._get_year_links()

    assert "2018/" in years
    assert "2019/" in years
    assert "readme.txt" not in years


def test_fetch_all_records_parsing(mock_requests_get, mocker):
    """Test full parsing logic of TIF filenames."""
    mock_requests_get.side_effect = [
        # Response 1: Main Page
        mocker.Mock(text='<a href="2020/">2020</a>', status_code=200),
        # Response 2: 2020 Page
        mocker.Mock(
            text='<a href="WD_MERGE_2020-01-01---2020-01-05_cluster_10.tif">File</a>',
            status_code=200,
        ),
    ]

    scraper = ScraperService()
    records = scraper.fetch_all_records()

    assert len(records) == 1
    rec = records[0]
    assert rec["year"] == "2020"
    assert rec["cluster_id"] == "10"
    assert rec["start_date"] == "2020-01-01"
    assert rec["end_date"] == "2020-01-05"
    assert rec["download_url"].endswith(
        "/2020/WD_MERGE_2020-01-01---2020-01-05_cluster_10.tif"
    )


def test_scraper_network_error(mock_requests_get):
    """Test error handling when base URL fails."""
    mock_requests_get.side_effect = RequestException("Boom")

    scraper = ScraperService()

    with pytest.raises(ScrapingError):
        scraper._get_year_links()


def test_fetch_records_ignored_and_errors(mock_requests_get, mocker, caplog):
    """Test ignoring non-tif files and handling year-level errors."""
    mock_requests_get.side_effect = [
        # Main Page
        mocker.Mock(
            text='<a href="2020/">2020</a><a href="2021/">2021</a>', status_code=200
        ),
        # 2020 Page: Contains a readme (ignored) and a bad file (ignored)
        mocker.Mock(
            text='<a href="readme.txt">Read</a><a href="bad_pattern.tif">Bad</a>',
            status_code=200,
        ),
        # 2021 Page: Raises Exception
        Exception("Server Error"),
    ]

    scraper = ScraperService()
    records = scraper.fetch_all_records()

    assert len(records) == 0
    # Check that 2021 error was logged but didn't crash scraper
    assert "scraping_failed_year" in caplog.text


def test_parse_link_size_no_parent_tr():
    """A link not inside a <tr> yields None (Apache listings without tables)."""
    link = BeautifulSoup('<a href="file.tif">f</a>', "html.parser").a
    assert ScraperService._parse_link_size(link) is None


def test_parse_link_size_row_without_size_cell():
    """A row whose cells are all non-size (date, name) returns None."""
    html = (
        "<table><tr>"
        '<td><a href="WD_MERGE.tif">f</a></td>'
        "<td>2024-01-01 12:00</td>"
        "<td>-</td>"
        "</tr></table>"
    )
    link = BeautifulSoup(html, "html.parser").a
    assert ScraperService._parse_link_size(link) is None


def test_fetch_all_records_size_bytes_none_when_no_table(mock_requests_get, mocker):
    """Without a surrounding table the record still parses; size_bytes is None."""
    mock_requests_get.side_effect = [
        mocker.Mock(text='<a href="2020/">2020</a>', status_code=200),
        mocker.Mock(
            text='<a href="WD_MERGE_2020-01-01---2020-01-05_cluster_10.tif">f</a>',
            status_code=200,
        ),
    ]
    records = ScraperService().fetch_all_records()
    assert records[0]["size_bytes"] is None


def test_fetch_all_records_global_id_collision_raises(mock_requests_get, mocker):
    """Two distinct filenames hashing to the same global_id fail fast."""
    mock_requests_get.side_effect = [
        mocker.Mock(text='<a href="2020/">2020</a>', status_code=200),
        mocker.Mock(
            text=(
                '<a href="WD_MERGE_2020-01-01---2020-01-05_cluster_1.tif">a</a>'
                '<a href="WD_MERGE_2020-02-01---2020-02-05_cluster_2.tif">b</a>'
            ),
            status_code=200,
        ),
    ]
    mocker.patch("euroflood.services.scraper._stable_global_id", return_value=42)

    with pytest.raises(ScrapingError, match="global_id collision"):
        ScraperService().fetch_all_records()
