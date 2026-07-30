import argparse
import hashlib
import logging
import os
import signal
import sqlite3
import sys
import time
from typing import Optional

from .cache import Cache
from .config import ConfigError, load_config
from .lastfm import (
    LastfmClient,
    canonicalize_tag_name,
    flatten_alias_groups,
    load_alias_groups,
    load_banlist,
    save_alias_groups,
    save_banlist,
)
from .scanner import run_once, scan_artist, wipe_all_genre_tags

log = logging.getLogger(__name__)

SHUTDOWN_POLL_SECONDS = 1.0  # шаг, которым дробим scan_interval, чтобы SIGTERM/SIGINT прерывали сон быстро

# Разделитель в файле алиасов для --add-alias-file: "главный тег <- вариант1, вариант2".
# Тот же вид, что печатает --list-aliases, чтобы вывод можно было скопировать в файл.
ALIAS_FILE_SEPARATOR = "<-"


def _apply_alias_pair(
    groups: dict[str, list[str]],
    variant: str,
    main_tag: str,
    banned: frozenset[str],
    banlist_path: str,
) -> Optional[str]:
    """Добавляет пару вариант -> главный тег в groups (на месте), проверяя всё,
    что может сделать алиас бесполезным или неожиданным. Возвращает None при
    успехе или текст ошибки, если пару добавлять нельзя. Используется и
    --add-alias, и --add-alias-file, чтобы правила были одни и те же."""
    if variant == main_tag:
        return f"{variant!r} canonicalizes to itself, variant and main genre must differ"

    # Алиас на забаненное имя молча обходил бы бан (см. _filter_tags) — ловим
    # конфликт здесь, где его видно, а не в рантайме на каждом теге.
    if variant in banned:
        return (
            f"{variant!r} is in the genre banlist ({banlist_path}) — remove it from there "
            "first, otherwise the ban wins and this alias would never apply"
        )
    if main_tag in banned:
        return (
            f"{main_tag!r} is in the genre banlist ({banlist_path}) — aliasing to a banned "
            "genre would just drop the tag; unban it first or pick another target"
        )

    # Цепочки (a -> b, где b сам вариант c) разрешаются при загрузке, но
    # результат почти никогда не совпадает с ожиданием — не даём их создать.
    if variant in groups:
        return (
            f"{variant!r} is already a main genre with variant(s) {groups[variant]} — adding it "
            f"as a variant of {main_tag!r} would create a chain; merge those variants into "
            f"{main_tag!r} instead"
        )
    owner_of_main = next((c for c, v in groups.items() if main_tag in v), None)
    if owner_of_main is not None:
        return (
            f"{main_tag!r} is itself a variant of {owner_of_main!r} — pointing {variant!r} at it "
            f"would create a chain; use {owner_of_main!r} as the target instead"
        )

    previous_owner = next((c for c, v in groups.items() if variant in v), None)
    if previous_owner == main_tag:
        log.info("Genre alias %r -> %r already present, nothing to do", variant, main_tag)
        return None
    if previous_owner is not None:
        log.warning("Moving variant %r from %r to %r", variant, previous_owner, main_tag)
        groups[previous_owner] = [v for v in groups[previous_owner] if v != variant]
        if not groups[previous_owner]:
            del groups[previous_owner]

    groups.setdefault(main_tag, []).append(variant)
    return None


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


def _banlist_fingerprint(banned: frozenset[str]) -> str:
    return hashlib.sha256(repr(sorted(banned)).encode()).hexdigest()[:16]


def _rewrite_on_config_change(cache: Cache, lastfm: LastfmClient, config_hash: str) -> None:
    """Вопрос 1 из PLAN.md: если MIN_TAG_COUNT/MAX_GENRES изменились с прошлого
    запуска (или изменился словарь GENRE_ALIASES_FILE / GENRE_BANLIST_FILE — см.
    _aliases_fingerprint/_banlist_fingerprint в вызывающем коде), пересчитываем
    фильтрацию тегов локально из уже сохранённого raw_response (без обращения к
    Last.fm) и форсируем перезапись ID3 у всех затронутых артистов на следующем
    run_once. Если после пересчёта у артиста не осталось ни одного жанра
    (например, все попали в бан-лист), genre в кэше станет None, а
    scan_artist/_strip_genre_from_files снимет тег с уже затегированных файлов."""
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
        metavar=("VARIANT", "MAIN"),
        help=(
            "Добавить синоним жанра в GENRE_ALIASES_FILE: тег VARIANT будет "
            "заменяться на MAIN при следующем пересчёте (например --add-alias "
            "hiphop 'hip hop'). В файле хранится группами: MAIN -> список "
            "своих вариантов. Ничего не сканирует — только правит файл; "
            "перезапись ID3 у уже обработанных артистов произойдёт на "
            "следующем обычном запуске."
        ),
    )
    parser.add_argument(
        "--add-alias-file",
        metavar="PATH",
        help=(
            "Применить пачку алиасов из файла, подготовленного "
            "scripts/dump_tags.py --suggest-aliases-file: по одной группе на "
            "строку в виде 'главный тег <- вариант1, вариант2', '#' и всё после "
            "него — комментарий, пустые строки игнорируются. Проверки те же, "
            "что у --add-alias; если хотя бы одна строка не проходит, не "
            "применяется ничего (чтобы не остаться с половиной правок)."
        ),
    )
    parser.add_argument(
        "--remove-alias",
        metavar="VARIANT",
        help=(
            "Убрать вариант написания из GENRE_ALIASES_FILE (обратная операция "
            "к --add-alias). Группа удаляется целиком, если в ней не осталось "
            "вариантов. Как и --add-alias, только правит файл."
        ),
    )
    parser.add_argument(
        "--list-aliases", action="store_true", help="Показать текущий словарь синонимов жанров и выйти"
    )
    parser.add_argument(
        "--ban-genre",
        nargs="+",
        metavar="GENRE",
        help=(
            "Добавить один или несколько жанров в бан-лист (GENRE_BANLIST_FILE), "
            "например --ban-genre trumpet icelandic belarussian: забаненный жанр "
            "больше не будет проставляться новым исполнителям и будет снят с "
            "ID3 и из БД у всех уже обработанных, у кого он встречается — "
            "эффект применяется на следующем обычном запуске (--once), как и "
            "при смене MIN_TAG_COUNT/MAX_GENRES/словаря синонимов. Ничего не "
            "сканирует само по себе — только правит файл."
        ),
    )
    parser.add_argument(
        "--ban-genre-file",
        metavar="PATH",
        help=(
            "Применить отредактированный список кандидатов из scripts/dump_tags.py "
            "--suggest-bans-file: по одному жанру на строку, '#' и всё после него "
            "на строке — комментарий, пустые строки игнорируются. Все жанры из "
            "файла добавляются в бан-лист (мержится с уже существующим), как и "
            "--ban-genre. Ничего не сканирует само по себе."
        ),
    )
    parser.add_argument(
        "--list-banned-genres", action="store_true", help="Показать текущий бан-лист жанров и выйти"
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
        "--limit",
        type=int,
        metavar="N",
        help=(
            "Обработать только первые N исполнителей (по алфавиту) за этот "
            "проход вместо всей библиотеки — удобно для пробного запуска "
            "перед полным включением. Работает вместе с --once/--force-scan. "
            "Уже обработанные ранее исполнители по-прежнему пропускаются по "
            "mtime-gate, так что N считается по каталогам, а не по реально "
            "затронутым артистам."
        ),
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

    if args.limit is not None and args.limit <= 0:
        log.error("--limit must be a positive integer, got %d", args.limit)
        sys.exit(1)

    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Invalid configuration: %s", exc)
        sys.exit(1)

    if args.add_alias:
        variant_raw, main_raw = args.add_alias
        variant = canonicalize_tag_name(variant_raw)
        main_tag = canonicalize_tag_name(main_raw)
        if not variant or not main_tag:
            log.error("Both --add-alias arguments must be non-empty")
            sys.exit(1)

        groups = load_alias_groups(config.genre_aliases_path)
        banned = load_banlist(config.genre_banlist_path)
        error = _apply_alias_pair(groups, variant, main_tag, banned, config.genre_banlist_path)
        if error:
            log.error("%s", error)
            sys.exit(1)

        save_alias_groups(config.genre_aliases_path, groups)
        log.info(
            "Added genre alias %r -> %r to %s (%d main genre(s), %d variant(s) total)",
            variant,
            main_tag,
            config.genre_aliases_path,
            len(groups),
            sum(len(v) for v in groups.values()),
        )
        return

    if args.add_alias_file:
        if not os.path.isfile(args.add_alias_file):
            log.error("No such file: %s", args.add_alias_file)
            sys.exit(1)

        groups = load_alias_groups(config.genre_aliases_path)
        banned = load_banlist(config.genre_banlist_path)
        errors: list[str] = []
        applied = 0
        with open(args.add_alias_file, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                if ALIAS_FILE_SEPARATOR not in line:
                    errors.append(
                        f"line {lineno}: expected 'MAIN {ALIAS_FILE_SEPARATOR} variant1, variant2', got {line!r}"
                    )
                    continue
                main_raw, variants_raw = line.split(ALIAS_FILE_SEPARATOR, 1)
                main_tag = canonicalize_tag_name(main_raw)
                if not main_tag:
                    errors.append(f"line {lineno}: main genre is empty")
                    continue
                variants = [canonicalize_tag_name(v) for v in variants_raw.split(",")]
                variants = [v for v in variants if v]
                if not variants:
                    errors.append(f"line {lineno}: no variants listed for {main_tag!r}")
                    continue
                for variant in variants:
                    error = _apply_alias_pair(
                        groups, variant, main_tag, banned, config.genre_banlist_path
                    )
                    if error:
                        errors.append(f"line {lineno}: {error}")
                    else:
                        applied += 1

        if errors:
            # Всё-или-ничего: пачечная правка не должна применяться частично,
            # иначе непонятно, что уже записано, а что надо поправить и повторить.
            log.error(
                "%d problem(s) in %s, nothing was written:", len(errors), args.add_alias_file
            )
            for error in errors:
                log.error("  %s", error)
            sys.exit(1)
        if not applied:
            log.warning("No alias groups found in %s, dictionary not changed", args.add_alias_file)
            return

        save_alias_groups(config.genre_aliases_path, groups)
        log.info(
            "Applied %d alias(es) from %s to %s (%d main genre(s), %d variant(s) total)",
            applied,
            args.add_alias_file,
            config.genre_aliases_path,
            len(groups),
            sum(len(v) for v in groups.values()),
        )
        return

    if args.remove_alias:
        variant = canonicalize_tag_name(args.remove_alias)
        if not variant:
            log.error("--remove-alias argument must be non-empty")
            sys.exit(1)
        groups = load_alias_groups(config.genre_aliases_path)
        owner = next((c for c, v in groups.items() if variant in v), None)
        if owner is None:
            log.error("No such alias variant: %r (see --list-aliases)", variant)
            sys.exit(1)
        groups[owner] = [v for v in groups[owner] if v != variant]
        if not groups[owner]:
            del groups[owner]
        save_alias_groups(config.genre_aliases_path, groups)
        log.info(
            "Removed genre alias %r -> %r from %s (%d main genre(s), %d variant(s) left)",
            variant,
            owner,
            config.genre_aliases_path,
            len(groups),
            sum(len(v) for v in groups.values()),
        )
        return

    if args.list_aliases:
        groups = load_alias_groups(config.genre_aliases_path)
        if not groups:
            print(f"Словарь синонимов пуст ({config.genre_aliases_path})")
        else:
            for main_tag, variants in sorted(groups.items()):
                print(f"{main_tag} <- {', '.join(variants)}")
        return

    if args.ban_genre:
        canonicals = [canonicalize_tag_name(g) for g in args.ban_genre]
        if not all(canonicals):
            log.error("--ban-genre arguments must be non-empty")
            sys.exit(1)
        banned = set(load_banlist(config.genre_banlist_path))
        banned.update(canonicals)
        save_banlist(config.genre_banlist_path, frozenset(banned))
        log.info(
            "Added %s to genre banlist %s (%d total)",
            ", ".join(repr(c) for c in canonicals),
            config.genre_banlist_path,
            len(banned),
        )
        return

    if args.ban_genre_file:
        if not os.path.isfile(args.ban_genre_file):
            log.error("No such file: %s", args.ban_genre_file)
            sys.exit(1)
        canonicals = set()
        with open(args.ban_genre_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                canonical = canonicalize_tag_name(line)
                if canonical:
                    canonicals.add(canonical)
        if not canonicals:
            log.warning("No genres found in %s, banlist not changed", args.ban_genre_file)
            return
        banned = set(load_banlist(config.genre_banlist_path))
        banned.update(canonicals)
        save_banlist(config.genre_banlist_path, frozenset(banned))
        log.info(
            "Added %d genre(s) from %s to genre banlist %s (%d total)",
            len(canonicals),
            args.ban_genre_file,
            config.genre_banlist_path,
            len(banned),
        )
        return

    if args.list_banned_genres:
        banned = load_banlist(config.genre_banlist_path)
        if not banned:
            print(f"Бан-лист жанров пуст ({config.genre_banlist_path})")
        else:
            for genre in sorted(banned):
                print(genre)
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

    alias_groups = load_alias_groups(config.genre_aliases_path)
    aliases = flatten_alias_groups(alias_groups)
    banned = load_banlist(config.genre_banlist_path)
    # CLI не даёт создать пересечение алиасов с бан-листом, но файлы могли
    # поправить руками — сообщаем один раз за запуск, а не на каждом теге.
    # В рантайме бан выигрывает (см. _filter_tags), т.е. алиас просто не
    # сработает, и без этого предупреждения это выглядело бы необъяснимо.
    alias_ban_conflicts = sorted(
        (set(aliases) | set(aliases.values())) & set(banned)
    )
    if alias_ban_conflicts:
        log.warning(
            "These genre(s) appear both in the alias dictionary and in the banlist: %s. "
            "The ban wins, so those aliases will not apply — remove them from %s or "
            "from %s to resolve the conflict.",
            ", ".join(repr(name) for name in alias_ban_conflicts),
            config.genre_banlist_path,
            config.genre_aliases_path,
        )
    lastfm = LastfmClient(
        config.lastfm_api_key,
        config.min_tag_count,
        config.max_genres,
        aliases=aliases,
        banned=banned,
        ban_after_top_n=config.ban_after_top_n,
    )

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

    combined_config_hash = f"{config.config_hash}:{_aliases_fingerprint(aliases)}:{_banlist_fingerprint(banned)}"
    _rewrite_on_config_change(cache, lastfm, combined_config_hash)

    if args.once:
        run_once(config, cache, lastfm, force_scan=args.force_scan, limit=args.limit)
        return

    stop = _install_shutdown_handler()
    while not stop["requested"]:
        run_once(config, cache, lastfm, force_scan=args.force_scan, limit=args.limit)
        if stop["requested"]:
            break
        _sleep_interruptibly(config.scan_interval_seconds, stop)
    log.info("Shutdown complete")


if __name__ == "__main__":
    main()
