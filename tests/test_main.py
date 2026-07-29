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


def _config_with_aliases_path(tmp_path, aliases_path, banlist_path=None):
    return Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=1,
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        genre_aliases_path=str(aliases_path),
        # Явный tmp-путь, а не дефолтный /data/... — --add-alias читает бан-лист,
        # чтобы поймать конфликт, и не должен зависеть от ФС хоста.
        genre_banlist_path=str(banlist_path or tmp_path / "banlist.json"),
        skip_dirs=frozenset(),
    )


def test_add_alias_creates_file_with_canonicalized_group(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "HipHop", "Hip-Hop"])

    main_module.main()

    from src.lastfm import load_alias_groups

    assert load_alias_groups(str(aliases_path)) == {"hip hop": ["hiphop"]}


def test_add_alias_appends_second_variant_to_same_group(tmp_path, monkeypatch):
    """Ключевая выгода группового формата: несколько вариантов одного главного
    тега лежат в одной записи, а не размазаны по всему файлу."""
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "matalcore", "metalcore"])

    main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["matalcore", "mathcore"]}


def test_add_alias_merges_into_existing_file(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"drum and bass": ["dnb"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "ska-core", "ska punk"])

    main_module.main()

    assert load_alias_groups(str(aliases_path)) == {
        "drum and bass": ["dnb"],
        "ska punk": ["ska core"],
    }


def test_add_alias_rejects_empty_argument(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "   ", "hip hop"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not aliases_path.exists()


def test_add_alias_rejects_identical_arguments(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "Ska-Punk", "ska punk"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not aliases_path.exists()


def test_add_alias_rejects_banned_variant(tmp_path, monkeypatch):
    """Иначе алиас молча не сработал бы: в _filter_tags бан выигрывает."""
    aliases_path = tmp_path / "aliases.json"
    banlist_path = tmp_path / "banlist.json"
    from src.lastfm import save_banlist

    save_banlist(str(banlist_path), frozenset({"trumpet"}))
    config = _config_with_aliases_path(tmp_path, aliases_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "trumpet", "jazz"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not aliases_path.exists()


def test_add_alias_rejects_banned_target(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    banlist_path = tmp_path / "banlist.json"
    from src.lastfm import save_banlist

    save_banlist(str(banlist_path), frozenset({"jazz"}))
    config = _config_with_aliases_path(tmp_path, aliases_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "trumpet", "jazz"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert not aliases_path.exists()


def test_add_alias_rejects_chain_when_variant_is_already_a_main_genre(tmp_path, monkeypatch):
    """metalcore уже главный тег со своими вариантами; сделать его вариантом
    hardcore — значит создать цепочку mathcore -> metalcore -> hardcore."""
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "metalcore", "hardcore"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["mathcore"]}


def test_add_alias_rejects_chain_when_target_is_already_a_variant(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    # Целью указан 'mathcore', который сам является вариантом 'metalcore'.
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "math core rock", "mathcore"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["mathcore"]}


def test_add_alias_moves_variant_between_groups_with_warning(tmp_path, monkeypatch, caplog):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore", "matalcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "mathcore", "hardcore"])

    with caplog.at_level("WARNING"):
        main_module.main()

    assert load_alias_groups(str(aliases_path)) == {
        "metalcore": ["matalcore"],
        "hardcore": ["mathcore"],
    }
    assert "Moving variant" in caplog.text


def test_add_alias_is_idempotent(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--add-alias", "mathcore", "metalcore"])

    main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["mathcore"]}


def test_remove_alias_drops_variant_and_empty_group(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"], "drum and bass": ["dnb"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--remove-alias", "MathCore"])

    main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"drum and bass": ["dnb"]}


def test_remove_alias_keeps_group_with_remaining_variants(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore", "matalcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--remove-alias", "mathcore"])

    main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["matalcore"]}


def test_remove_alias_rejects_unknown_variant(tmp_path, monkeypatch):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import load_alias_groups, save_alias_groups

    save_alias_groups(str(aliases_path), {"metalcore": ["mathcore"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--remove-alias", "nope"])

    with pytest.raises(SystemExit):
        main_module.main()

    assert load_alias_groups(str(aliases_path)) == {"metalcore": ["mathcore"]}


def test_list_aliases_prints_groups_sorted(tmp_path, monkeypatch, capsys):
    aliases_path = tmp_path / "aliases.json"
    from src.lastfm import save_alias_groups

    save_alias_groups(str(aliases_path), {"zeta genre": ["z1"], "hip hop": ["hiphop", "hip-hop2"]})
    config = _config_with_aliases_path(tmp_path, aliases_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--list-aliases"])

    main_module.main()

    out = capsys.readouterr().out
    assert out.index("hip hop") < out.index("zeta genre")
    assert "hip hop <- hip hop2, hiphop" in out


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


def test_ban_genre_file_applies_edited_candidate_list(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    candidates_path = tmp_path / "candidates.txt"
    candidates_path.write_text(
        "# Кандидаты на бан\n"
        "\n"
        "trumpet  # 2 track(s), 1 artist(s)\n"
        "icelandic  # 3 track(s), 1 artist(s)\n",
        encoding="utf-8",
    )
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre-file", str(candidates_path)])

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"trumpet", "icelandic"}


def test_ban_genre_file_respects_manually_removed_lines(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    candidates_path = tmp_path / "candidates.txt"
    # Пользователь вручную удалил строку с icelandic, оставив только trumpet.
    candidates_path.write_text("trumpet  # 2 track(s), 1 artist(s)\n", encoding="utf-8")
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre-file", str(candidates_path)])

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"trumpet"}


def test_ban_genre_file_merges_with_existing_banlist(tmp_path, monkeypatch):
    from src.lastfm import save_banlist

    banlist_path = tmp_path / "banlist.json"
    save_banlist(str(banlist_path), frozenset({"already banned"}))
    candidates_path = tmp_path / "candidates.txt"
    candidates_path.write_text("trumpet\n", encoding="utf-8")
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre-file", str(candidates_path)])

    main_module.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"already banned", "trumpet"}


def test_ban_genre_file_rejects_missing_file(tmp_path, monkeypatch):
    banlist_path = tmp_path / "banlist.json"
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(
        "sys.argv", ["genre-tagger", "--ban-genre-file", str(tmp_path / "missing.txt")]
    )

    with pytest.raises(SystemExit):
        main_module.main()

    assert not banlist_path.exists()


def test_ban_genre_file_warns_when_only_comments_and_blank_lines(tmp_path, monkeypatch, caplog):
    banlist_path = tmp_path / "banlist.json"
    candidates_path = tmp_path / "candidates.txt"
    candidates_path.write_text("# всё удалено\n\n", encoding="utf-8")
    config = _config_with_banlist_path(tmp_path, banlist_path)
    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr("sys.argv", ["genre-tagger", "--ban-genre-file", str(candidates_path)])

    with caplog.at_level("WARNING"):
        main_module.main()

    assert not banlist_path.exists()


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
