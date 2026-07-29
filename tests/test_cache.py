import sqlite3
from datetime import datetime, timedelta, timezone

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


def test_is_stale_false_for_unknown_artist(cache):
    assert cache.is_stale("Nobody", ttl_days=1) is False


def test_is_stale_false_when_within_ttl(cache):
    timestamp = datetime.now(timezone.utc).isoformat()
    cache.mark_done("Artist", ["rock"], {}, None, timestamp)
    assert cache.is_stale("Artist", ttl_days=180) is False


def test_is_stale_true_when_past_ttl(cache):
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    cache.mark_done("Artist", ["rock"], {}, None, old_timestamp)
    assert cache.is_stale("Artist", ttl_days=180) is True


def test_list_artists_returns_all_known_artists(cache):
    cache.mark_done("Artist A", ["rock"], {}, None, "t")
    cache.mark_done("Artist B", None, {}, None, "t")
    assert sorted(cache.list_artists()) == ["Artist A", "Artist B"]


def test_iter_genres_yields_artist_and_parsed_genre_list(cache):
    cache.mark_done("Artist A", ["rock", "pop"], {}, None, "t")
    cache.mark_done("Artist B", None, {}, None, "t")
    assert sorted(cache.iter_genres()) == [("Artist A", ["rock", "pop"]), ("Artist B", None)]


def test_iter_genre_track_counts_sums_files_across_albums(cache):
    albums = {
        "Album 1": {"mtime": 0.0, "files": ["a.mp3", "b.mp3"]},
        "Album 2": {"mtime": 0.0, "files": ["c.mp3"]},
    }
    cache.mark_done("Artist A", ["rock"], albums, None, "t")
    cache.mark_done("Artist B", None, {}, None, "t")

    result = {artist: (genres, count) for artist, genres, count in cache.iter_genre_track_counts()}

    assert result == {
        "Artist A": (["rock"], 3),
        "Artist B": (None, 0),
    }


def test_wipe_all_clears_artists_force_rewrite_and_config_hash(cache):
    cache.mark_done("Artist", ["rock"], {}, None, "t")
    cache.set_force_rewrite("Artist")
    cache.set_config_hash("10:3:abc")

    cache.wipe_all()

    assert cache.list_artists() == []
    assert cache.is_done("Artist") is False
    assert cache.is_force_rewrite("Artist") is False
    assert cache.get_config_hash() is None
