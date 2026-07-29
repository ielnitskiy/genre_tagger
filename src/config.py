import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    music_dir: str
    db_path: str
    lastfm_api_key: str
    scan_interval_seconds: int
    min_tag_count: int
    max_genres: int
    skip_dirs: frozenset[str]

    @property
    def config_hash(self) -> str:
        # Только параметры, влияющие на результат фильтрации тегов —
        # смена интервала/пути не должна форсировать пересчёт жанров.
        return f"{self.min_tag_count}:{self.max_genres}"


DEFAULT_SKIP_DIRS = frozenset({"download-errors"})


def load_config() -> Config:
    api_key = os.environ["LASTFM_API_KEY"]
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
        scan_interval_seconds=int(os.environ.get("SCAN_INTERVAL_SECONDS", "86400")),
        min_tag_count=int(os.environ.get("MIN_TAG_COUNT", "10")),
        max_genres=int(os.environ.get("MAX_GENRES", "3")),
        skip_dirs=skip_dirs,
    )
