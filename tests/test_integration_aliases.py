"""Сквозной сценарий: библиотека уже полностью прометена с 'дублирующимся'
написанием жанра, пользователь дописывает строку в aliases.txt, и на следующем
обычном прогоне (main._rewrite_on_config_change + run_once) все уже
затегированные файлы получают канонический жанр — без единого нового обращения
к Last.fm."""

from mutagen.easyid3 import EasyID3

from src import main as main_module
from src import scanner
from src.cache import Cache
from src.genrelists import fingerprint, load_aliases
from src.lastfm import LastfmClient
from src.tagger import write_genre
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


def _hash(aliases=None):
    return f"{main_module.PIPELINE_VERSION}:{fingerprint(frozenset(), aliases or {})}"


def test_adding_alias_retags_already_scanned_library_without_network_calls(tmp_path):
    music_dir = tmp_path / "music"
    db_path = tmp_path / "genres.db"
    aliases_path = tmp_path / "aliases.txt"

    # 1. Артист уже полностью прометён раньше: в кэше лежит genre="hiphop" и
    #    полный raw_response, файл уже затегирован старым написанием.
    album_path = _make_album(music_dir, "Some Rapper", "Album", ["a.mp3"])
    cache = Cache(str(db_path))
    raw_json = '{"toptags": {"tag": [{"name": "hiphop", "count": "50"}]}}'
    cache.mark_done("Some Rapper", ["hiphop"], {}, raw_json, "2025-01-01T00:00:00Z")
    write_genre(str(album_path / "a.mp3"), ["hiphop"])
    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["hiphop"]
    # Артист уже был обработан под конфигом без алиасов — фиксируем это в БД,
    # иначе _rewrite_on_config_change сочтёт это первым запуском и не станет
    # ничего пересчитывать (см. семантику stored_hash is None в main.py).
    cache.set_config_hash(_hash())

    # 2. Пользователь замечает дубль и дописывает строку в aliases.txt.
    aliases_path.write_text("hip hop <- hiphop\n", encoding="utf-8")
    aliases = load_aliases(str(aliases_path))

    # 3. Следующий обычный прогон: main._rewrite_on_config_change пересчитывает
    #    жанры всех артистов из сохранённого raw_response с новым словарём —
    #    LastfmClient, который бросает исключение при любой сетевой попытке,
    #    доказывает, что Last.fm при этом не дёргается.
    lastfm = NetworkForbiddenLastfm(api_key="unused", aliases=aliases)
    main_module._rewrite_on_config_change(cache, lastfm, _hash(aliases))

    genres, _albums = cache.get("Some Rapper")
    assert genres == ["hip hop"]
    assert cache.is_force_rewrite("Some Rapper") is True

    # 4. Обычный scan_artist подхватывает force_rewrite и перезаписывает файл
    #    новым жанром, снова не обращаясь к Last.fm (mtime папки не менялся).
    scanner.scan_artist("Some Rapper", str(music_dir), cache, lastfm)

    assert EasyID3(str(album_path / "a.mp3"))["genre"] == ["hip hop"]
    assert cache.is_force_rewrite("Some Rapper") is False

    cache.close()
