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
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        genre_aliases_path="",
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


def _config_with_aliases_path(tmp_path, aliases_path):
    return Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=1,
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        genre_aliases_path=str(aliases_path),
        skip_dirs=frozenset(),
    )


def test_add_alias_creates_file_with_canonicalized_entry(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "HipHop", "Hip-Hop"])

    main_module.main()

    from src.lastfm import load_aliases

    assert load_aliases(str(aliases_path)) == {"hiphop": "hip hop"}


def test_add_alias_merges_into_existing_file(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import save_aliases

    save_aliases(str(aliases_path), {"dnb": "drum and bass"})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "ska-punk", "ska punk"])

    main_module.main()

    from src.lastfm import load_aliases

    assert load_aliases(str(aliases_path)) == {
        "dnb": "drum and bass",
        "ska punk": "ska punk",
    }


def test_add_alias_rejects_empty_argument(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "   ", "hip hop"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not aliases_path.exists()


def test_list_aliases_prints_sorted_entries(tmp_path, monkeypatch, capsys):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import save_aliases

    save_aliases(str(aliases_path), {"zeta tag": "z genre", "hiphop": "hip hop"})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--list-aliases"])

    main_module.main()

    out = capsys.readouterr().out
    assert out.index("hiphop") < out.index("zeta tag")


def test_list_aliases_reports_empty_dictionary(tmp_path, monkeypatch, capsys):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--list-aliases"])

    main_module.main()

    assert "пуст" in capsys.readouterr().out


def test_aliases_fingerprint_changes_when_aliases_change():
    fp_a = main_module._aliases_fingerprint({"hiphop": "hip hop"})
    fp_b = main_module._aliases_fingerprint({"hiphop": "hip-hop"})
    fp_same = main_module._aliases_fingerprint({"hiphop": "hip hop"})
    assert fp_a == fp_same
    assert fp_a != fp_b


def _config_with_banlist_path(tmp_path, banlist_path):
    return Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=1,
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        genre_aliases_path=str(tmp_path / "aliases.json"),
        genre_banlist_path=str(banlist_path),
        skip_dirs=frozenset(),
    )


def test_ban_genre_creates_file_with_canonicalized_entry(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre", "K-Pop"])

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"k pop"}


def test_ban_genre_accepts_multiple_values_in_one_call(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        "sys.argv", ["genre-tagger", "--ban-genre", "trumpet", "Icelandic", "belarussian"]
    )

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"trumpet", "icelandic", "belarussian"}


def test_ban_genre_merges_into_existing_file(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    from src.lastfm import save_banlist

    save_banlist(str(banlist_path), frozenset({"pop"}))
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre", "disco"])

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"pop", "disco"}


def test_ban_genre_rejects_empty_argument(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre", "   "])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not banlist_path.exists()


def test_list_banned_genres_prints_sorted_entries(tmp_path, monkeypatch, capsys):
    banlist_path = tmp_path / "banlist.json"
    from src.lastfm import save_banlist

    save_banlist(str(banlist_path), frozenset({"zeta genre", "alpha genre"}))
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--list-banned-genres"])

    main_module.main()

    out = capsys.readouterr().out
    assert out.index("alpha genre") < out.index("zeta genre")


def test_list_banned_genres_reports_empty_list(tmp_path, monkeypatch, capsys):
    banlist_path = tmp_path / "banlist.json"
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--list-banned-genres"])

    main_module.main()

    assert "пуст" in capsys.readouterr().out


def test_banlist_fingerprint_changes_when_banlist_changes():
    fp_a = main_module._banlist_fingerprint(frozenset({"pop"}))
    fp_b = main_module._banlist_fingerprint(frozenset({"pop", "disco"}))
    fp_same = main_module._banlist_fingerprint(frozenset({"pop"}))
    assert fp_a == fp_same
    assert fp_a != fp_b


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

    config = _config_with_aliases_path(tmp_path, tmp_path / "aliases.json")
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
    config = _config_with_aliases_path(tmp_path, tmp_path / "aliases.json")

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
