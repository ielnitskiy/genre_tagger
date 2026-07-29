import pytest

from src.config import ConfigError, load_config


def _clear_env(monkeypatch):
    for name in (
        "LASTFM_API_KEY",
        "MUSIC_DIR",
        "DB_PATH",
        "SCAN_INTERVAL_SECONDS",
        "MIN_TAG_COUNT",
        "MAX_GENRES",
        "GENRE_TTL_DAYS",
        "GENRE_ALIASES_FILE",
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


@pytest.mark.parametrize("var", ["SCAN_INTERVAL_SECONDS", "MIN_TAG_COUNT", "MAX_GENRES"])
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
    assert config.min_tag_count == 10
    assert config.max_genres == 3
    assert config.genre_ttl_days == 180
    assert config.genre_aliases_path == "/data/genre_aliases.json"
    assert config.skip_dirs == frozenset({"download-errors"})


def test_genre_aliases_path_overridable(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv("GENRE_ALIASES_FILE", "/custom/aliases.json")
    config = load_config()
    assert config.genre_aliases_path == "/custom/aliases.json"


def test_skip_dirs_parsed_from_csv(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv("SKIP_DIRS", " Various Artists , download-errors ,,soundtracks")
    config = load_config()
    assert config.skip_dirs == frozenset({"Various Artists", "download-errors", "soundtracks"})


def test_config_hash_depends_only_on_tag_filtering_params(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("LASTFM_API_KEY", "key")
    monkeypatch.setenv("MIN_TAG_COUNT", "20")
    monkeypatch.setenv("MAX_GENRES", "5")
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "60")
    config_a = load_config()

    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "999")
    config_b = load_config()

    assert config_a.config_hash == config_b.config_hash == "20:5"
