"""Сквозной сценарий: артист уже затегирован жанром, который затем попадает в
бан-лист через --ban-genre. На следующем обычном прогоне
(main._rewrite_on_config_change + run_once) жанр должен исчезнуть и из БД, и
с уже проставленного ID3-тега — без единого нового обращения к Last.fm."""

from mutagen.easyid3 import EasyID3

from src import main as main_module
from src import scanner
from src.cache import Cache
from src.lastfm import LastfmClient, load_banlist, save_banlist
from src.tagger import write_genre
from tests.conftest import make_mp3


class NetworkForbiddenLastfm(LastfmClient):
    def _fetch_top_tags(self, artist):
        raise AssertionError(f"unexpected Last.fm network call for {artist!r} during offline recompute")


def _make_album(music_dir, artist, album, filenames):
    album_path = music_dir / artist / album
    album_path.mkdir(parents=True)
    for name in filenames:
        make_mp3(album_path / name)
    return album_path


def test_banning_genre_strips_it_from_already_tagged_library_without_network_calls(tmp_path):
    music_dir = tmp_path / "music"
    db_path = tmp_path / "genres.db"
    banlist_path = tmp_path / "banlist.json"

    # 1. Артист уже полностью прометён раньше: genre=["pop"], файл уже затегирован.
    album_path = _make_album(music_dir, "Some Singer", "Album", ["a.mp3"])
    cache = Cache(str(db_path))
    raw_json = '{"toptags": {"tag": [{"name": "pop", "count": "50"}]}}'
    cache.mark_done("Some Singer", ["pop"], {}, raw_json, "2025-01-01T00:00:00Z")
    write_genre(str(album_path / "a.mp3"), ["pop"])
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["pop"]
    cache.set_config_hash(
        f"1:3:{main_module._aliases_fingerprint({})}:{main_module._banlist_fingerprint(frozenset())}"
    )

    # 2. Пользователь банит жанр через --ban-genre.
    save_banlist(str(banlist_path), frozenset())
    banned = set(load_banlist(str(banlist_path)))
    banned.add("pop")
    save_banlist(str(banlist_path), frozenset(banned))

    # 3. Следующий обычный прогон пересчитывает жанры всех артистов из
    #    сохранённого raw_response с новым бан-листом — без сети.
    lastfm = NetworkForbiddenLastfm(
        api_key="unused", min_tag_count=1, max_genres=3, banned=load_banlist(str(banlist_path))
    )
    combined_hash = (
        f"1:3:{main_module._aliases_fingerprint({})}:{main_module._banlist_fingerprint(lastfm._banned)}"
    )
    main_module._rewrite_on_config_change(cache, lastfm, combined_hash)

    genres, _albums = cache.get("Some Singer")
    assert genres is None
    assert cache.is_force_rewrite("Some Singer") is True

    # 4. Обычный scan_artist подхватывает force_rewrite и снимает тег с файла,
    #    снова не обращаясь к Last.fm (mtime папки не менялся).
    scanner.scan_artist("Some Singer", str(music_dir), cache, lastfm)

    assert "genre" not in EasyID3(str(album_path / "a.mp3"))
    assert cache.is_force_rewrite("Some Singer") is False

    cache.close()


def test_banning_one_of_several_genres_keeps_the_rest_tagged(tmp_path):
    music_dir = tmp_path / "music"
    db_path = tmp_path / "genres.db"

    album_path = _make_album(music_dir, "Some Band", "Album", ["a.mp3"])
    cache = Cache(str(db_path))
    raw_json = (
        '{"toptags": {"tag": [{"name": "rock", "count": "50"}, '
        '{"name": "pop", "count": "40"}]}}'
    )
    cache.mark_done("Some Band", ["rock", "pop"], {}, raw_json, "2025-01-01T00:00:00Z")
    write_genre(str(album_path / "a.mp3"), ["rock", "pop"])
    cache.set_config_hash(
        f"1:3:{main_module._aliases_fingerprint({})}:{main_module._banlist_fingerprint(frozenset())}"
    )

    lastfm = NetworkForbiddenLastfm(
        api_key="unused", min_tag_count=1, max_genres=3, banned=frozenset({"pop"})
    )
    combined_hash = (
        f"1:3:{main_module._aliases_fingerprint({})}:{main_module._banlist_fingerprint(lastfm._banned)}"
    )
    main_module._rewrite_on_config_change(cache, lastfm, combined_hash)

    genres, _albums = cache.get("Some Band")
    assert genres == ["rock"]

    scanner.scan_artist("Some Band", str(music_dir), cache, lastfm)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["rock"]

    cache.close()