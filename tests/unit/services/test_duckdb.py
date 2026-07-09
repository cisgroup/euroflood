"""Tests for the per-thread cached DuckDB connection (services._duckdb)."""

from euroflood.services import _duckdb


def test_connection_is_reused_within_a_thread(mocker):
    """Repeated ``connection()`` calls reuse one connection (opened once)."""
    _duckdb.close_cached_connections()  # start from a clean slate
    spy = mocker.spy(_duckdb.duckdb, "connect")
    first = _duckdb.connection()
    second = _duckdb.connection()
    assert first is second
    assert spy.call_count == 1  # not reopened per lookup


def test_close_drops_the_cached_connection():
    """After close, the next call opens a fresh connection."""
    first = _duckdb.connection()
    _duckdb.close_cached_connections()
    second = _duckdb.connection()
    assert first is not second


def test_close_is_idempotent():
    """Closing when nothing is cached is a no-op (safe cleanup path)."""
    _duckdb.close_cached_connections()
    _duckdb.close_cached_connections()  # no error


def test_repositories_share_one_connection(realdata_index_env, mocker):
    """Dictionary + events lookups reuse the single cached connection, not one each."""
    from euroflood.services.dictionary_repository import DictionaryRepository
    from euroflood.services.events_repository import EventsRepository

    _duckdb.close_cached_connections()
    spy = mocker.spy(_duckdb.duckdb, "connect")
    DictionaryRepository(settings=realdata_index_env).lookup_combos([1])
    EventsRepository(settings=realdata_index_env).lookup_events([1])
    assert spy.call_count == 1  # both repos hit the same reused connection
