"""Сквозной сценарий: артист уже затегирован жанром, который затем дописывают в
banlist.txt. На следующем обычном прогоне (main._rewrite_on_config_change +
run_once) жанр должен исчезнуть и из БД, и с уже проставленного ID3-тега — без
единого нового обращения к Last.fm."""

from mutagen.easyid3 import EasyID3

from src import main as main_module
from src import scanner
from src.cache import Cache
from src.genrelists import fingerprint, load_banlist
from src.lastfm import LastfmClient
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


def _hash(banned=frozenset(), aliases=None):
    return f"{main_module.PIPELINE_VERSION}:{fingerprint(banned, aliases or {})}"


def test_banning_genre_strips_it_from_already_tagged_library_without_network_calls(tmp_path):
    music_dir = tmp_path / "music"
    db_path = tmp_path / "genres.db"
    banlist_path = tmp_path / "banlist.txt"

    # 1. Артист уже полностью прометён раньше: genre=["pop"], файл уже затегирован.
    album_path = _make_album(music_dir, "Some Singer", "Album", ["a.mp3"])
    cache = Cache(str(db_path))
    raw_json = '{"toptags": {"tag": [{"name": "pop", "count": "50"}]}}'
    cache.mark_done("Some Singer", ["pop"], {}, raw_json, "2025-01-01T00:00:00Z")
    write_genre(str(album_path / "a.mp3"), ["pop"])
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["pop"]
    cache.set_config_hash(_hash())

    # 2. Пользователь дописывает жанр в banlist.txt обычным редактором.
    banlist_path.write_text("pop\n", encoding="utf-8")
    banned = load_banlist(str(banlist_path))

    # 3. Следующий обычный прогон пересчитывает жанры всех артистов из
    #    сохранённого raw_response с новым бан-листом — без сети.
    lastfm = NetworkForbiddenLastfm(api_key="unused", banned=banned)
    main_module._rewrite_on_config_change(cache, lastfm, _hash(banned))

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
    cache.set_config_hash(_hash())

    banned = frozenset({"pop"})
    lastfm = NetworkForbiddenLastfm(api_key="unused", banned=banned)
    main_module._rewrite_on_config_change(cache, lastfm, _hash(banned))

    genres, _albums = cache.get("Some Band")
    assert genres == ["rock"]

    scanner.scan_artist("Some Band", str(music_dir), cache, lastfm)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["rock"]

    cache.close()


def test_banned_genre_does_not_pull_a_fourth_tag_into_id3(tmp_path):
    """Регрессия на главную причину упрощения конвейера: бан не должен поднимать
    в ID3 тег из хвоста, иначе после бан-прохода жанров в библиотеке становится
    больше, а не меньше."""
    music_dir = tmp_path / "music"
    album_path = _make_album(music_dir, "Some Band", "Album", ["a.mp3"])
    cache = Cache(str(tmp_path / "genres.db"))
    raw_json = (
        '{"toptags": {"tag": ['
        '{"name": "rock", "count": "100"}, {"name": "russian", "count": "90"}, '
        '{"name": "punk", "count": "80"}, {"name": "garage rock", "count": "70"}]}}'
    )
    cache.mark_done("Some Band", ["rock", "russian", "punk"], {}, raw_json, "2025-01-01T00:00:00Z")
    write_genre(str(album_path / "a.mp3"), ["rock", "russian", "punk"])
    cache.set_config_hash(_hash())

    banned = frozenset({"russian"})
    lastfm = NetworkForbiddenLastfm(api_key="unused", banned=banned)
    main_module._rewrite_on_config_change(cache, lastfm, _hash(banned))

    genres, _albums = cache.get("Some Band")
    assert genres == ["rock", "punk"]
    assert "garage rock" not in genres

    cache.close()
