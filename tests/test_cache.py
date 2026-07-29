import sqlite3

import pytest

from src.cache import Cache


@pytest.fixture
def cache(tmp_path):
    c = Cache(str(tmp_path / "genres.db"))
    yield c
    c.close()


def test_unknown_artist_is_not_done(cache):
    assert cache.is_done("Nobody") is False
    assert cache.get("Nobody") == (None, {})


def test_mark_done_then_get_roundtrips_genres_and_albums(cache):
    albums = {"Album A": {"mtime": 123.0, "files": ["a.mp3"]}}
    cache.mark_done("Artist", ["rock", "indie"], albums, raw_json='{"ok":1}', timestamp="2026-01-01T00:00:00Z")

    assert cache.is_done("Artist") is True
    genres, cached_albums = cache.get("Artist")
    assert genres == ["rock", "indie"]
    assert cached_albums == albums


def test_mark_done_with_none_genres_roundtrips_none(cache):
    cache.mark_done("Artist", None, {}, raw_json=None, timestamp="2026-01-01T00:00:00Z")
    genres, _albums = cache.get("Artist")
    assert genres is None


def test_update_albums_overwrites_previous_albums(cache):
    cache.mark_done("Artist", ["rock"], {"Old": {"mtime": 1.0, "files": []}}, None, "t")
    cache.update_albums("Artist", {"New": {"mtime": 2.0, "files": ["b.mp3"]}})
    _genres, albums = cache.get("Artist")
    assert albums == {"New": {"mtime": 2.0, "files": ["b.mp3"]}}


def test_update_genre_changes_only_genre_not_albums(cache):
    albums = {"Album": {"mtime": 1.0, "files": []}}
    cache.mark_done("Artist", ["old-genre"], albums, None, "t")
    cache.update_genre("Artist", ["new-genre"])
    genres, cached_albums = cache.get("Artist")
    assert genres == ["new-genre"]
    assert cached_albums == albums


def test_reset_removes_artist_entirely(cache):
    cache.mark_done("Artist", ["rock"], {}, None, "t")
    cache.reset("Artist")
    assert cache.is_done("Artist") is False


def test_force_rewrite_flag_survives_reset(cache):
    """Регрессия по дизайну: force_rewrite — отдельная таблица от artist_genre,
    так что --reset-artist не должен затронуть уже выставленный флаг."""
    cache.mark_done("Artist", ["rock"], {}, None, "t")
    cache.set_force_rewrite("Artist")
    cache.reset("Artist")
    assert cache.is_force_rewrite("Artist") is True

    cache.clear_force_rewrite("Artist")
    assert cache.is_force_rewrite("Artist") is False


def test_iter_with_raw_response_skips_artists_without_raw(cache):
    cache.mark_done("WithRaw", ["rock"], {}, raw_json='{"a":1}', timestamp="t")
    cache.mark_done("WithoutRaw", ["indie"], {}, raw_json=None, timestamp="t")

    results = dict(cache.iter_with_raw_response())
    assert results == {"WithRaw": '{"a":1}'}


def test_config_hash_defaults_to_none_then_persists(cache):
    assert cache.get_config_hash() is None
    cache.set_config_hash("10:3")
    assert cache.get_config_hash() == "10:3"
    cache.set_config_hash("20:5")
    assert cache.get_config_hash() == "20:5"


def test_opens_with_wal_journal_mode(tmp_path):
    db_path = str(tmp_path / "genres.db")
    c = Cache(db_path)
    try:
        mode = c._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        c.close()


def test_corrupt_db_file_raises_database_error(tmp_path):
    db_path = tmp_path / "genres.db"
    db_path.write_bytes(b"this is not a sqlite database")
    with pytest.raises(sqlite3.DatabaseError):
        Cache(str(db_path))
