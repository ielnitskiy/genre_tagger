import argparse
import logging
import sqlite3
import sys
import time

from .cache import Cache
from .config import ConfigError, load_config
from .lastfm import LastfmClient
from .scanner import run_once

log = logging.getLogger(__name__)


def _rewrite_on_config_change(cache: Cache, lastfm: LastfmClient, config_hash: str) -> None:
    """Вопрос 1 из PLAN.md: если MIN_TAG_COUNT/MAX_GENRES изменились с прошлого
    запуска, пересчитываем фильтрацию тегов локально из уже сохранённого
    raw_response (без обращения к Last.fm) и форсируем перезапись ID3 у всех
    затронутых артистов на следующем run_once."""
    stored_hash = cache.get_config_hash()
    if stored_hash == config_hash:
        return
    if stored_hash is not None:
        log.info(
            "Config hash changed (%s -> %s), recomputing genres from stored raw_response",
            stored_hash,
            config_hash,
        )
        for artist, raw_json in cache.iter_with_raw_response():
            new_genres = lastfm.parse_tags(raw_json)
            cache.update_genre(artist, new_genres)
            cache.set_force_rewrite(artist)
    cache.set_config_hash(config_hash)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="genre_tagger: проставляет жанры Last.fm в ID3-теги")
    parser.add_argument("--once", action="store_true", help="Один проход и выход (без цикла)")
    parser.add_argument("--reset-artist", metavar="ARTIST", help="Удалить артиста из кэша и форсировать пересчёт/перезапись")
    parser.add_argument(
        "--force-scan",
        action="store_true",
        help=(
            "Игнорировать mtime-gate и честно перечитать (listdir) каждый альбом "
            "каждого исполнителя на этом проходе, не дёргая Last.fm повторно и не "
            "перезаписывая уже проставленные теги. Страховка на случай, если ФС "
            "не обновляет mtime папки надёжно (см. известное ограничение про NFS/SMB)."
        ),
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Invalid configuration: %s", exc)
        sys.exit(1)

    try:
        cache = Cache(config.db_path)
    except sqlite3.DatabaseError as exc:
        log.error("Cannot open cache database at %s: %s", config.db_path, exc)
        sys.exit(1)

    lastfm = LastfmClient(config.lastfm_api_key, config.min_tag_count, config.max_genres)

    if args.reset_artist:
        cache.reset(args.reset_artist)
        cache.set_force_rewrite(args.reset_artist)
        log.info("Reset cache entry for artist %r, forcing rewrite on next scan", args.reset_artist)
        return

    _rewrite_on_config_change(cache, lastfm, config.config_hash)

    if args.once:
        run_once(config, cache, lastfm, force_scan=args.force_scan)
        return

    while True:
        run_once(config, cache, lastfm, force_scan=args.force_scan)
        time.sleep(config.scan_interval_seconds)


if __name__ == "__main__":
    main()
