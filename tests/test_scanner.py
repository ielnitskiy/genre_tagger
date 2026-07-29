import os
import time

import pytest
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

from src import scanner
from src.cache import Cache
from tests.conftest import make_corrupt_id3, make_mp3


class FakeLastfm:
    """Заглушка вместо реального LastfmClient — считает вызовы и не трогает сеть."""

    def __init__(self, genres=("rock", "indie")):
        self.genres = list(genres) if genres is not None else None
        self.calls = []

    def resolve_genres(self, artist):
        self.calls.append(artist)
        return self.genres, '{"fake":"raw"}'


@pytest.fixture
def cache(tmp_path):
    c = Cache(str(tmp_path / "genres.db"))
    yield c
    c.close()


def _make_album(music_dir, artist, album, filenames):
    album_path = music_dir / artist / album
    album_path.mkdir(parents=True)
    for name in filenames:
        make_mp3(album_path / name)
    return album_path


def test_new_artist_resolves_genres_and_tags_all_files(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3", "b.mp3"])
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == ["Artist"]
    assert cache.is_done("Artist") is True
    for name in ("a.mp3", "b.mp3"):
        path = music_dir / "Artist" / "Album" / name
        assert EasyID3(str(path))["genre"] == ["rock"]


def test_artist_with_no_albums_is_skipped_entirely(tmp_path, cache):
    music_dir = tmp_path / "music"
    (music_dir / "Artist").mkdir(parents=True)
    lastfm = FakeLastfm()

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == []
    assert cache.is_done("Artist") is False


def test_unchanged_artist_does_not_requery_lastfm_or_retag(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == ["Artist"]  # только один раз, второй скан — no-op


def test_new_album_added_later_reuses_cached_genres_without_new_lastfm_call(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album1", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    _make_album(music_dir, "Artist", "Album2", ["c.mp3"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == ["Artist"]  # жанры не перезапрашивались
    path = music_dir / "Artist" / "Album2" / "c.mp3"
    assert EasyID3(str(path))["genre"] == ["rock"]


def test_new_file_added_to_existing_album_gets_tagged(tmp_path, cache):
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    # мтайм папки должен измениться, иначе gate не увидит новый файл
    make_mp3(album_path / "b.mp3")
    os.utime(album_path, None)

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert EasyID3(str(album_path / "b.mp3"))["genre"] == ["rock"]
    assert lastfm.calls == ["Artist"]


def test_none_genres_from_lastfm_are_cached_and_no_tagging_happens(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=None)

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert cache.is_done("Artist") is True
    genres, _albums = cache.get("Artist")
    assert genres is None
    path = music_dir / "Artist" / "Album" / "a.mp3"
    with pytest.raises(ID3NoHeaderError):
        EasyID3(str(path))  # тег вообще не писался — файл остался без ID3-заголовка


def test_force_rewrite_retags_unchanged_files_without_new_lastfm_call(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    cache.update_genre("Artist", ["updated-genre"])
    cache.set_force_rewrite("Artist")

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    path = music_dir / "Artist" / "Album" / "a.mp3"
    assert EasyID3(str(path))["genre"] == ["updated-genre"]
    assert lastfm.calls == ["Artist"]  # force-rewrite не должен дёргать Last.fm
    assert cache.is_force_rewrite("Artist") is False  # флаг снят после применения


def test_failed_tag_marks_album_for_recheck_next_scan(tmp_path, cache):
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Artist", "Album", [])
    make_corrupt_id3(album_path / "broken.mp3")
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    _genres, albums = cache.get("Artist")
    assert albums["Album"]["mtime"] == scanner.FORCE_RECHECK_MTIME


def test_force_scan_relists_files_without_forcing_retag(tmp_path, cache):
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    # ставим другой жанр вручную, чтобы убедиться, что force_scan не перезаписывает его
    path = album_path / "a.mp3"
    tags = EasyID3(str(path))
    tags["genre"] = ["manually-set"]
    tags.save(str(path), v2_version=4)

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, force_scan=True)

    assert EasyID3(str(path))["genre"] == ["manually-set"]
    assert lastfm.calls == ["Artist"]


def test_run_once_continues_after_unexpected_error_in_one_artist(tmp_path, cache, monkeypatch):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "ArtistA", "Album", ["a.mp3"])
    _make_album(music_dir, "ArtistB", "Album", ["b.mp3"])
    lastfm = FakeLastfm(genres=["rock"])

    original_scan_artist = scanner.scan_artist

    def boom(artist_name, *args, **kwargs):
        if artist_name == "ArtistA":
            raise RuntimeError("simulated failure")
        return original_scan_artist(artist_name, *args, **kwargs)

    monkeypatch.setattr(scanner, "scan_artist", boom)

    from src.config import Config

    config = Config(
        music_dir=str(music_dir),
        db_path="unused",
        lastfm_api_key="key",
        scan_interval_seconds=1,
        min_tag_count=1,
        max_genres=1,
        skip_dirs=frozenset(),
    )

    scanner.run_once(config, cache, lastfm)

    assert cache.is_done("ArtistA") is False
    assert cache.is_done("ArtistB") is True
