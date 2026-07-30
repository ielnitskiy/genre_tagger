import os
from dataclasses import dataclass


class ConfigError(Exception):
    """Некорректная или отсутствующая конфигурация окружения."""


@dataclass(frozen=True)
class Config:
    music_dir: str
    db_path: str
    lastfm_api_key: str
    scan_interval_seconds: int
    genre_ttl_days: int
    banlist_path: str
    aliases_path: str
    skip_dirs: frozenset[str] = frozenset()


DEFAULT_SKIP_DIRS = frozenset({"download-errors"})


def _int_env(name: str, default: str) -> int:
    raw = os.environ.get(name, default)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None


def load_config() -> Config:
    try:
        api_key = os.environ["LASTFM_API_KEY"]
    except KeyError:
        raise ConfigError("LASTFM_API_KEY environment variable is required") from None
    if not api_key.strip():
        raise ConfigError("LASTFM_API_KEY environment variable must not be empty")

    skip_dirs_raw = os.environ.get("SKIP_DIRS", "")
    skip_dirs = (
        frozenset(x.strip() for x in skip_dirs_raw.split(",") if x.strip())
        if skip_dirs_raw
        else DEFAULT_SKIP_DIRS
    )
    return Config(
        music_dir=os.environ.get("MUSIC_DIR", "/music"),
        db_path=os.environ.get("DB_PATH", "/data/genres.db"),
        lastfm_api_key=api_key,
        scan_interval_seconds=_int_env("SCAN_INTERVAL_SECONDS", "86400"),
        genre_ttl_days=_int_env("GENRE_TTL_DAYS", "180"),
        banlist_path=os.environ.get("BANLIST_FILE", "/data/banlist.txt"),
        aliases_path=os.environ.get("ALIASES_FILE", "/data/aliases.txt"),
        skip_dirs=skip_dirs,
    )
