import argparse
import hashlib
import logging
import os
import signal
import sqlite3
import sys
import time

from .cache import Cache
from .config import ConfigError, load_config
from .lastfm import LastfmClient, canonicalize_tag_name, load_aliases, save_aliases
from .scanner import run_once, scan_artist, wipe_all_genre_tags

log = logging.getLogger(__name__)

SHUTDOWN_POLL_SECONDS = 1.0  # шаг, которым дробим scan_interval, чтобы SIGTERM/SIGINT прерывали сон быстро


def _resolve_log_level() -> int:
    raw = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(raw)
    if not isinstance(level, int):
        print(f"Unknown LOG_LEVEL {raw!r}, falling back to INFO", file=sys.stderr)
        return logging.INFO
    return level


def _install_shutdown_handler():
    stop = {"requested": False}

    def _handle(signum, _frame):
        log.info("Received signal %d, will stop after the current scan pass", signum)
        stop["requested"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    return stop


def _sleep_interruptibly(seconds: float, stop: dict) -> None:
    remaining = seconds
    while remaining > 0 and not stop["requested"]:
        time.sleep(min(SHUTDOWN_POLL_SECONDS, remaining))
        remaining -= SHUTDOWN_POLL_SECONDS


def _aliases_fingerprint(aliases: dict[str, str]) -> str:
    return hashlib.sha256(repr(sorted(aliases.items())).encode()).hexdigest()[:16]


def _rewrite_on_config_change(cache: Cache, lastfm: LastfmClient, config_hash: str) -> None:
    """Вопрос 1 из PLAN.md: если MIN_TAG_COUNT/MAX_GENRES изменились с прошлого
    запуска (или изменился словарь GENRE_ALIASES_FILE — см. _aliases_fingerprint
    в вызывающем коде), пересчитываем фильтрацию тегов локально из уже
    сохранённого raw_response (без обращения к Last.fm) и форсируем перезапись
    ID3 у всех затронутых артистов на следующем run_once."""
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
        level=_resolve_log_level(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="genre_tagger: проставляет жанры Last.fm в ID3-теги")
    parser.add_argument("--once", action="store_true", help="Один проход и выход (без цикла)")
    parser.add_argument("--reset-artist", metavar="ARTIST", help="Удалить артиста из кэша и форсировать пересчёт/перезапись")
    parser.add_argument(
        "--tag-artist",
        metavar="ARTIST",
        help=(
            "Обработать одного исполнителя прямо сейчас: сбросить его кэш и "
            "сразу сходить в Last.fm/перезаписать ID3, без полного скана "
            "остальной библиотеки. ARTIST должен точно совпадать с именем "
            "папки в MUSIC_DIR. Эквивалент --reset-artist + --once, но "
            "затрагивает только этого исполнителя."
        ),
    )
    parser.add_argument(
        "--add-alias",
        nargs=2,
        metavar=("FROM", "TO"),
        help=(
            "Добавить синоним жанра в GENRE_ALIASES_FILE: теги, канонизированные "
            "к FROM, будут заменяться на TO при следующем пересчёте (например "
            "--add-alias hiphop 'hip hop'). Ничего не сканирует — только правит "
            "файл; перезапись ID3 у уже обработанных артистов произойдёт на "
            "следующем обычном запуске."
        ),
    )
    parser.add_argument(
        "--list-aliases", action="store_true", help="Показать текущий словарь синонимов жанров и выйти"
    )
    parser.add_argument(
        "--wipe-all-genres",
        action="store_true",
        help=(
            "ОПАСНО: снять genre-тег со ВСЕХ mp3 в MUSIC_DIR и полностью сбросить "
            "кэш (все артисты станут 'новыми'). Без --yes ничего не меняет — "
            "только показывает, сколько файлов затронет."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Подтверждение для --wipe-all-genres (без него это dry-run)",
    )
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

    if args.add_alias:
        from_tag, to_tag = args.add_alias
        canonical_from = canonicalize_tag_name(from_tag)
        canonical_to = canonicalize_tag_name(to_tag)
        if not canonical_from or not canonical_to:
            log.error("Both --add-alias arguments must be non-empty")
            sys.exit(1)
        aliases = load_aliases(config.genre_aliases_path)
        aliases[canonical_from] = canonical_to
        save_aliases(config.genre_aliases_path, aliases)
        log.info(
            "Added genre alias %r -> %r to %s (%d total)",
            canonical_from,
            canonical_to,
            config.genre_aliases_path,
            len(aliases),
        )
        return

    if args.list_aliases:
        aliases = load_aliases(config.genre_aliases_path)
        if not aliases:
            print(f"Словарь синонимов пуст ({config.genre_aliases_path})")
        else:
            for from_tag, to_tag in sorted(aliases.items()):
                print(f"{from_tag} -> {to_tag}")
        return

    if args.wipe_all_genres:
        scanned, affected, failed = wipe_all_genre_tags(config.music_dir, dry_run=not args.yes)
        if not args.yes:
            log.warning(
                "Dry run: %d/%d mp3 file(s) under %s currently have a genre tag "
                "(%d unreadable, skipped). Nothing was changed. Re-run with --yes "
                "to actually strip tags from disk and fully reset the cache.",
                affected,
                scanned,
                config.music_dir,
                failed,
            )
            sys.exit(1)
        try:
            cache = Cache(config.db_path)
        except sqlite3.DatabaseError as exc:
            log.error("Cannot open cache database at %s: %s", config.db_path, exc)
            sys.exit(1)
        cache.wipe_all()
        cache.close()
        log.info(
            "Wiped genre tag from %d/%d mp3 file(s) under %s (%d unreadable, skipped) "
            "and fully reset the cache",
            affected,
            scanned,
            config.music_dir,
            failed,
        )
        return

    try:
        cache = Cache(config.db_path)
    except sqlite3.DatabaseError as exc:
        log.error("Cannot open cache database at %s: %s", config.db_path, exc)
        sys.exit(1)

    aliases = load_aliases(config.genre_aliases_path)
    lastfm = LastfmClient(config.lastfm_api_key, config.min_tag_count, config.max_genres, aliases=aliases)

    if args.reset_artist:
        cache.reset(args.reset_artist)
        cache.set_force_rewrite(args.reset_artist)
        log.info("Reset cache entry for artist %r, forcing rewrite on next scan", args.reset_artist)
        return

    if args.tag_artist:
        artist_path = os.path.join(config.music_dir, args.tag_artist)
        if not os.path.isdir(artist_path):
            log.error("No such artist directory: %s", artist_path)
            sys.exit(1)
        cache.reset(args.tag_artist)
        cache.set_force_rewrite(args.tag_artist)
        scan_artist(
            args.tag_artist,
            config.music_dir,
            cache,
            lastfm,
            force_scan=True,
            genre_ttl_days=config.genre_ttl_days,
        )
        log.info("Finished tagging artist %r", args.tag_artist)
        return

    combined_config_hash = f"{config.config_hash}:{_aliases_fingerprint(aliases)}"
    _rewrite_on_config_change(cache, lastfm, combined_config_hash)

    if args.once:
        run_once(config, cache, lastfm, force_scan=args.force_scan)
        return

    stop = _install_shutdown_handler()
    while not stop["requested"]:
        run_once(config, cache, lastfm, force_scan=args.force_scan)
        if stop["requested"]:
            break
        _sleep_interruptibly(config.scan_interval_seconds, stop)
    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
