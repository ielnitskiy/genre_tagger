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
    min_tag_count: int
    max_genres: int
    genre_ttl_days: int
    genre_aliases_path: str
    genre_banlist_path: str = "/data/genre_banlist.json"
    skip_dirs: frozenset[str] = frozenset()

    @property
    def config_hash(self) -> str:
        # Только параметры, влияющие на результат фильтрации тегов —
        # смена интервала/пути не должна форсировать пересчёт жанров.
        return f"{self.min_tag_count}:{self.max_genres}"


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
        min_tag_count=_int_env("MIN_TAG_COUNT", "10"),
        max_genres=_int_env("MAX_GENRES", "3"),
        genre_ttl_days=_int_env("GENRE_TTL_DAYS", "180"),
        genre_aliases_path=os.environ.get("GENRE_ALIASES_FILE", "/data/genre_aliases.json"),
        genre_banlist_path=os.environ.get("GENRE_BANLIST_FILE", "/data/genre_banlist.json"),
        skip_dirs=skip_dirs,
    )
