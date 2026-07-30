import os
import signal

import pytest

from src import main as main_module
from src.config import Config


def test_resolve_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert main_module._resolve_log_level() == 20  # logging.INFO


def test_resolve_log_level_accepts_valid_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert main_module._resolve_log_level() == 10  # logging.DEBUG


def test_resolve_log_level_falls_back_to_info_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
    assert main_module._resolve_log_level() == 20  # logging.INFO
    assert "NOT_A_LEVEL" in capsys.readouterr().err


def test_sleep_interruptibly_stops_early_when_flag_already_set():
    stop = {"requested": True}
    # Не должен спать вовсе — цикл проверяет флаг перед каждым time.sleep.
    main_module._sleep_interruptibly(1000.0, stop)


def test_sleep_interruptibly_stops_after_flag_set_mid_sleep(monkeypatch):
    stop = {"requested": False}
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            stop["requested"] = True

    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)
    main_module._sleep_interruptibly(10.0, stop)

    assert len(calls) == 2  # остановились сразу после второго тика, не проспав все 10с


def test_daemon_loop_stops_after_current_scan_pass_on_sigterm(tmp_path, monkeypatch):
    config = Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=10_000,  # заведомо больше, чем должен реально проспать тест
        genre_ttl_days=180,
        banlist_path=str(tmp_path / "banlist.txt"),
        aliases_path=str(tmp_path / "aliases.txt"),
        skip_dirs=frozenset(),
    )
    (tmp_path / "music").mkdir()

    run_once_calls = []

    def fake_run_once(*_args, **_kwargs):
        run_once_calls.append(1)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "LastfmClient", lambda *a, **kw: object())
    monkeypatch.setattr(main_module, "run_once", fake_run_once)
    monkeypatch.setattr("sys.argv", ["genre-tagger"])

    main_module.main()

    assert run_once_calls == [1]  # цикл не начал второй проход и не залип в _sleep_interruptibly


@pytest.fixture(autouse=True)
def _restore_default_signal_handlers():
    yield
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)


def _config(tmp_path, banlist_path=None, aliases_path=None):
    return Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=1,
        genre_ttl_days=180,
        banlist_path=str(banlist_path or tmp_path / "banlist.txt"),
        aliases_path=str(aliases_path or tmp_path / "aliases.txt"),
        skip_dirs=frozenset(),
    )


def _make_tagged_mp3(path):
    from mutagen.easyid3 import EasyID3

    from tests.conftest import make_mp3

    make_mp3(path)
    tags = EasyID3()
    tags["genre"] = ["Rock"]
    tags.save(str(path), v2_version=4)


def test_wipe_all_genres_without_yes_is_a_dry_run(tmp_path, monkeypatch, capsys):
    music_dir = tmp_path / "music" / "Artist" / "Album"
    music_dir.mkdir(parents=True)
    _make_tagged_mp3(music_dir / "a.mp3")

    config = _config(tmp_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--wipe-all-genres"])

    with pytest.raises(SystemExit):
        main_module.main()

    from mutagen.easyid3 import EasyID3

    assert EasyID3(str(music_dir / "a.mp3"))["genre"] == ["Rock"]  # ничего не изменилось
    assert not (tmp_path / "genres.db").exists()  # кэш даже не открывался


def test_wipe_all_genres_with_yes_strips_tags_and_resets_cache(tmp_path, monkeypatch):
    music_dir = tmp_path / "music" / "Artist" / "Album"
    music_dir.mkdir(parents=True)
    _make_tagged_mp3(music_dir / "a.mp3")

    db_path = tmp_path / "genres.db"
    config = _config(tmp_path)

    from src.cache import Cache

    cache = Cache(str(db_path))
    cache.mark_done("Artist", ["rock"], {}, None, "t")
    cache.close()

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--wipe-all-genres", "--yes"])

    main_module.main()

    from mutagen.easyid3 import EasyID3

    assert "genre" not in EasyID3(str(music_dir / "a.mp3"))

    cache = Cache(str(db_path))
    try:
        assert cache.is_done("Artist") is False
    finally:
        cache.close()


def test_report_prints_genres_and_similar_pairs(tmp_path, monkeypatch, capsys):
    from src.cache import Cache

    cache = Cache(str(tmp_path / "genres.db"))
    albums = {"Album": {"mtime": 1.0, "files": ["a.mp3", "b.mp3"]}}
    cache.mark_done("Rapper", ["hip hop"], albums, None, "t")
    cache.mark_done("Other Rapper", ["hiphop"], {"A": {"mtime": 1.0, "files": ["c.mp3"]}}, None, "t")
    cache.close()

    monkeypatch.setattr(main_module, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--report"])

    main_module.main()

    out = capsys.readouterr().out
    assert "2 трек(ов)" in out and "hip hop" in out
    assert "hip hop <- hiphop" in out  # подсказка для aliases.txt


def test_report_does_not_touch_the_network_or_the_lists(tmp_path, monkeypatch):
    from src.cache import Cache

    Cache(str(tmp_path / "genres.db")).close()

    def explode(*_args, **_kwargs):
        raise AssertionError("--report must not build a Last.fm client")

    monkeypatch.setattr(main_module, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main_module, "LastfmClient", explode)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--report"])

    main_module.main()

    assert not (tmp_path / "banlist.txt").exists()


def test_once_migrates_legacy_json_lists_on_first_run(tmp_path, monkeypatch):
    import json

    (tmp_path / "genre_banlist.json").write_text(json.dumps(["Pop"]), encoding="utf-8")
    (tmp_path / "genre_aliases.json").write_text(
        json.dumps({"hip hop": ["hiphop"]}), encoding="utf-8"
    )
    (tmp_path / "music").mkdir()

    monkeypatch.setattr(main_module, "load_config", lambda: _config(tmp_path))
    monkeypatch.setattr(main_module, "run_once", lambda *a, **kw: None)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--once"])

    main_module.main()

    from src.genrelists import load_aliases, load_banlist

    assert load_banlist(str(tmp_path / "banlist.txt")) == frozenset({"pop"})
    assert load_aliases(str(tmp_path / "aliases.txt")) == {"hiphop": "hip hop"}
