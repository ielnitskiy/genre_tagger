import pytest

from src.config import ConfigError, load_config


def _clear_env(monkeypatch):
    for name in (
        "LASTFM_API_KEY",
        "MUSIC_DIR",
        "DB_PATH",
        "SCAN_INTERVAL_SECONDS",
        "GENRE_TTL_DAYS",
        "BANLIST_FILE",
        "ALIASES_FILE",
        "SKIP_DIRS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_api_key_raises_config_error(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ConfigError, match="LASTFM_API_KEY"):
        load_config()


def test_empty_api_key_raises_config_error(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "   ")
    with pytest.raises(ConfigError, match="LASTFM_API_KEY"):
        load_config()


@pytest.mark.parametrize("var", ["SCAN_INTERVAL_SECONDS", "GENRE_TTL_DAYS"])
def test_invalid_int_env_var_raises_config_error(monkeypatch, var):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv(var, "not-a-number")
    with pytest.raises(ConfigError, match=var):
        load_config()


def test_defaults_applied(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    config = load_config()
    assert config.music_dir == "/music"
    assert config.db_path == "/data/genres.db"
    assert config.scan_interval_seconds == 86400
    assert config.genre_ttl_days == 180
    assert config.banlist_path == "/data/banlist.txt"
    assert config.aliases_path == "/data/aliases.txt"
    assert config.skip_dirs == frozenset({"download-errors"})


def test_list_paths_are_overridable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv("BANLIST_FILE", "/custom/ban.txt")
    monkeypatch.setenv("ALIASES_FILE", "/custom/al.txt")
    config = load_config()
    assert config.banlist_path == "/custom/ban.txt"
    assert config.aliases_path == "/custom/al.txt"


def test_skip_dirs_parsed_from_csv(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv("SKIP_DIRS", " Various Artists , download-errors ,,soundtracks")
    config = load_config()
    assert config.skip_dirs == frozenset({"Various Artists", "download-errors", "soundtracks"})
