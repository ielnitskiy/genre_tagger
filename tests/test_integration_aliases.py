"""Сквозной сценарий: библиотека уже полностью прометена с 'дублирующимся'
написанием жанра, пользователь добавляет синоним через --add-alias, и на
следующем обычном прогоне (main._rewrite_on_config_change + run_once) все
уже затегированные файлы получают канонический жанр — без единого нового
обращения к Last.fm."""

from mutagen.easyid3 import EasyID3

from src import main as main_module
from src import scanner
from src.cache import Cache
from src.lastfm import LastfmClient, load_aliases, save_aliases
from tests.conftest import make_mp3


class NetworkForbiddenLastfm(LastfmClient):
    """Гарантирует, что во время пересчёта не будет ни одного HTTP-запроса."""

    def _fetch_top_tags(self, artist):
        raise AssertionError(f"unexpected Last.fm network call for {artist!r} during offline recompute")


def _make_album(music_dir, artist, album, filenames):
    album_path = music_dir / artist / album
    album_path.mkdir(parents=True)
    for name in filenames:
        make_mp3(album_path / name)
    return album_path


def test_adding_alias_retags_already_scanned_library_without_network_calls(tmp_path):
    music_dir = tmp_path / "music"
    db_path = tmp_path / "genres.db"
    aliases_path = tmp_path / "aliases.json"

    # 1. Артист уже полностью прометён раньше: в кэше лежит genre="hiphop" и
    #    полный raw_response, файл уже затегирован старым написанием.
    album_path = _make_album(music_dir, "Some Rapper", "Album", ["a.mp3"])
    cache = Cache(str(db_path))
    raw_json = '{"toptags": {"tag": [{"name": "hiphop", "count": "50"}]}}'
    cache.mark_done("Some Rapper", ["hiphop"], {}, raw_json, "2025-01-01T00:00:00Z")
    from src.tagger import write_genre

    write_genre(str(album_path / "a.mp3"), ["hiphop"])
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["hiphop"]
    # Артист уже был обработан под конфигом без алиасов — фиксируем это в БД,
    # иначе _rewrite_on_config_change сочтёт это первым запуском и не станет
    # ничего пересчитывать (см. семантику stored_hash is None в main.py).
    cache.set_config_hash(f"1:3:{main_module._aliases_fingerprint({})}")

    # 2. Пользователь замечает дубль и добавляет синоним через --add-alias.
    save_aliases(str(aliases_path), {})
    aliases = load_aliases(str(aliases_path))
    aliases["hiphop"] = "hip hop"
    save_aliases(str(aliases_path), aliases)

    # 3. Следующий обычный прогон: main._rewrite_on_config_change пересчитывает
    #    жанры всех артистов из сохранённого raw_response с новым словарём —
    #    LastfmClient, который бросает исключение при любой сетевой попытке,
    #    доказывает, что Last.fm при этом не дёргается.
    lastfm = NetworkForbiddenLastfm(
        api_key="unused", min_tag_count=1, max_genres=3, aliases=load_aliases(str(aliases_path))
    )
    combined_hash = f"1:3:{main_module._aliases_fingerprint(lastfm._aliases)}"
    main_module._rewrite_on_config_change(cache, lastfm, combined_hash)

    genres, _albums = cache.get("Some Rapper")
    assert genres == ["hip hop"]
    assert cache.is_force_rewrite("Some Rapper") is True

    # 4. Обычный scan_artist подхватывает force_rewrite и перезаписывает файл
    #    новым жанром, снова не обращаясь к Last.fm (mtime папки не менялся).
    scanner.scan_artist("Some Rapper", str(music_dir), cache, lastfm)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["hip hop"]
    assert cache.is_force_rewrite("Some Rapper") is False

    cache.close()
