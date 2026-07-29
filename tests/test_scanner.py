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


def _backdate(cache, artist, days):
    from datetime import datetime, timedelta, timezone

    genres, albums = cache.get(artist)
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cache.mark_done(artist, genres, albums, '{"fake":"raw"}', old_timestamp)


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


def test_new_artist_with_no_real_files_is_skipped_without_lastfm_call(tmp_path, cache):
    """Фикс прод-инцидента: album_dirs непустой (папка альбома есть), но внутри
    нет ни одного .mp3 (неудачная/незавершённая закачка spotDL) — раньше
    scan_artist всё равно резолвил жанр через Last.fm по одному имени артиста
    и кэшировал его как 'done', что давало расхождение track_count <
    artist_count (см. test_final_counts_diverge_when_artist_has_genre_but_zero_real_tracks
    в test_dump_tags.py) и "осиротевшие" записи при удалении пустых папок.
    Теперь такой артист просто пропускается: ни сети, ни записи в кэш."""
    music_dir = tmp_path / "music"
    (music_dir / "Artist" / "Failed Download").mkdir(parents=True)  # папка есть, mp3 — нет
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == []
    assert cache.is_done("Artist") is False


def test_artist_gets_tagged_once_real_files_eventually_appear(tmp_path, cache):
    """Пропуск пустой закачки не блокирует артиста навсегда — как только
    реальный .mp3 появится в той же папке, следующий обычный скан должен
    подхватить его как нового артиста."""
    music_dir = tmp_path / "music"
    album_path = music_dir / "Artist" / "Album"
    album_path.mkdir(parents=True)
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)
    assert cache.is_done("Artist") is False

    make_mp3(album_path / "a.mp3")  # закачка "долилась"

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == ["Artist"]
    assert cache.is_done("Artist") is True
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["rock"]


def test_artist_with_one_empty_and_one_real_album_is_tagged_normally(tmp_path, cache):
    """Пропуск действует только когда РЕАЛЬНЫХ файлов 0 во ВСЕХ альбомах —
    если хотя бы один альбом с настоящими треками уже есть, артист
    обрабатывается как обычно (в т.ч. пустой альбом остаётся в кэше с пустым
    files-списком, просто не мешает тегированию остального)."""
    music_dir = tmp_path / "music"
    (music_dir / "Artist" / "Failed Download").mkdir(parents=True)
    _make_album(music_dir, "Artist", "Real Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm)

    assert lastfm.calls == ["Artist"]
    assert cache.is_done("Artist") is True
    assert EasyID3(str(music_dir / "Artist" / "Real Album" / "a.mp3"))["genre"] == ["rock"]


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
        genre_ttl_days=180,
        genre_aliases_path="",
        skip_dirs=frozenset(),
    )

    scanner.run_once(config, cache, lastfm)

    assert cache.is_done("ArtistA") is False
    assert cache.is_done("ArtistB") is True


def test_fresh_cache_does_not_requery_lastfm_even_with_ttl_set(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)

    assert lastfm.calls == ["Artist"]  # кэш свежий, TTL не истёк


def test_stale_cache_requeries_lastfm_and_refreshes_timestamp(tmp_path, cache):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)
    _backdate(cache, "Artist", days=200)
    assert cache.is_stale("Artist", ttl_days=180) is True

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)

    assert lastfm.calls == ["Artist", "Artist"]
    assert cache.is_stale("Artist", ttl_days=180) is False  # processed_at обновился


def test_stale_cache_with_changed_genre_retags_existing_files(tmp_path, cache):
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["rock"]

    _backdate(cache, "Artist", days=200)
    lastfm.genres = ["jazz"]

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["jazz"]
    genres, _albums = cache.get("Artist")
    assert genres == ["jazz"]


def test_stale_cache_with_unchanged_genre_does_not_error_and_keeps_genre(tmp_path, cache):
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    lastfm = FakeLastfm(genres=["rock"])
    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)
    _backdate(cache, "Artist", days=200)

    scanner.scan_artist("Artist", str(music_dir), cache, lastfm, genre_ttl_days=180)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["rock"]


def test_run_once_logs_warning_for_orphaned_cache_entry(tmp_path, cache, caplog):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "ArtistA", "Album", ["a.mp3"])
    _make_album(music_dir, "ArtistB", "Album", ["b.mp3"])
    lastfm = FakeLastfm(genres=["rock"])

    from src.config import Config

    config = Config(
        music_dir=str(music_dir),
        db_path="unused",
        lastfm_api_key="key",
        scan_interval_seconds=1,
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        genre_aliases_path="",
        skip_dirs=frozenset(),
    )
    scanner.run_once(config, cache, lastfm)

    import shutil

    shutil.rmtree(music_dir / "ArtistB")

    with caplog.at_level("WARNING"):
        scanner.run_once(config, cache, lastfm)

    assert any("ArtistB" in record.message for record in caplog.records)
    assert cache.is_done("ArtistB") is True  # запись не удаляется автоматически


def test_wipe_all_genre_tags_strips_genre_from_every_mp3(tmp_path):
    music_dir = tmp_path / "music"
    album_a = _make_album(music_dir, "ArtistA", "Album", ["a.mp3"])
    album_b = _make_album(music_dir, "ArtistB", "Album", ["b.mp3", "c.mp3"])
    for path in (album_a / "a.mp3", album_b / "b.mp3", album_b / "c.mp3"):
        tags = EasyID3()
        tags["genre"] = ["Rock"]
        tags.save(str(path), v2_version=4)

    scanned, affected, failed = scanner.wipe_all_genre_tags(str(music_dir))

    assert (scanned, affected, failed) == (3, 3, 0)
    for path in (album_a / "a.mp3", album_b / "b.mp3", album_b / "c.mp3"):
        assert "genre" not in EasyID3(str(path))


def test_wipe_all_genre_tags_dry_run_does_not_modify_files(tmp_path):
    music_dir = tmp_path / "music"
    album = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    path = album / "a.mp3"
    tags = EasyID3()
    tags["genre"] = ["Rock"]
    tags.save(str(path), v2_version=4)

    scanned, affected, failed = scanner.wipe_all_genre_tags(str(music_dir), dry_run=True)

    assert (scanned, affected, failed) == (1, 1, 0)
    assert EasyID3(str(path))["genre"] == ["Rock"]  # dry-run ничего не поменял


def test_wipe_all_genre_tags_counts_files_without_genre_as_unaffected(tmp_path):
    music_dir = tmp_path / "music"
    _make_album(music_dir, "Artist", "Album", ["a.mp3"])  # без genre вообще

    scanned, affected, failed = scanner.wipe_all_genre_tags(str(music_dir))

    assert (scanned, affected, failed) == (1, 0, 0)


def test_wipe_all_genre_tags_counts_corrupted_files_as_failed_and_continues(tmp_path):
    music_dir = tmp_path / "music"
    album = _make_album(music_dir, "Artist", "Album", ["a.mp3"])
    make_corrupt_id3(album / "broken.mp3")
    tags = EasyID3()
    tags["genre"] = ["Rock"]
    tags.save(str(album / "a.mp3"), v2_version=4)

    scanned, affected, failed = scanner.wipe_all_genre_tags(str(music_dir))

    assert (scanned, affected, failed) == (2, 1, 1)
    assert "genre" not in EasyID3(str(album / "a.mp3"))
