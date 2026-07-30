"""Бан-лист и словарь алиасов в виде обычных текстовых файлов.

Оба файла редактируются руками в любом редакторе — CLI-команд для их правки
нет намеренно: раньше их было семь, и все они существовали только затем, чтобы
менять два JSON-файла через docker.

    banlist.txt     по одному жанру на строку
    aliases.txt     'главный тег <- вариант1, вариант2'

В обоих файлах '#' и всё после него — комментарий, пустые строки игнорируются.
Всё содержимое канонизируется тем же canonicalize_tag_name(), что и теги
Last.fm, поэтому регистр и дефисы в файлах значения не имеют.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from src.tagnames import canonicalize_tag_name

log = logging.getLogger(__name__)

ALIAS_SEPARATOR = "<-"


def _iter_meaningful_lines(path: str):
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.split("#", 1)[0].strip()
            if line:
                yield lineno, line


def load_banlist(path: Optional[str]) -> frozenset[str]:
    """Жанры, которые никогда не попадают в ID3. Отсутствующий файл — не
    ошибка, просто список пуст."""
    if not path or not os.path.exists(path):
        return frozenset()
    try:
        return frozenset(
            canonical
            for _lineno, line in _iter_meaningful_lines(path)
            if (canonical := canonicalize_tag_name(line))
        )
    except OSError as exc:
        log.warning("Cannot read banlist %s: %s", path, exc)
        return frozenset()


def load_aliases(path: Optional[str], banned: frozenset[str] = frozenset()) -> dict[str, str]:
    """Плоская таблица подстановки 'вариант -> главный тег'.

    Проблемные строки не отменяют весь файл (как это делал --add-alias-file), а
    пропускаются с WARNING: файл правится руками, и из-за одной опечатки не
    должна вставать вся библиотека. Проверяются те же случаи, что раньше ловил
    CLI: самоссылка, цепочки, дубли варианта в двух группах, пересечение с
    бан-листом.
    """
    if not path or not os.path.exists(path):
        return {}

    main_of: dict[str, str] = {}
    try:
        lines = list(_iter_meaningful_lines(path))
    except OSError as exc:
        log.warning("Cannot read aliases %s: %s", path, exc)
        return {}

    for lineno, line in lines:
        if ALIAS_SEPARATOR not in line:
            log.warning(
                "%s:%d: expected 'main genre %s variant1, variant2', ignoring %r",
                path,
                lineno,
                ALIAS_SEPARATOR,
                line,
            )
            continue
        main_raw, _, variants_raw = line.partition(ALIAS_SEPARATOR)
        main_tag = canonicalize_tag_name(main_raw)
        if not main_tag:
            log.warning("%s:%d: main genre is empty, ignoring line", path, lineno)
            continue
        if main_tag in banned:
            log.warning(
                "%s:%d: main genre %r is in the banlist, ignoring line (the ban wins anyway)",
                path,
                lineno,
                main_tag,
            )
            continue
        for variant_raw in variants_raw.split(","):
            variant = canonicalize_tag_name(variant_raw)
            if not variant or variant == main_tag:
                continue
            if variant in banned:
                log.warning(
                    "%s:%d: %r is in the banlist, ignoring it as a variant of %r",
                    path,
                    lineno,
                    variant,
                    main_tag,
                )
                continue
            if variant in main_of and main_of[variant] != main_tag:
                log.warning(
                    "%s:%d: %r is already a variant of %r, ignoring the one pointing to %r",
                    path,
                    lineno,
                    variant,
                    main_of[variant],
                    main_tag,
                )
                continue
            main_of[variant] = main_tag

    return _resolve_chains(main_of, path)


def _resolve_chains(main_of: dict[str, str], path: str) -> dict[str, str]:
    """Разворачивает 'a -> b -> c' в 'a -> c'. Подстановка при фильтрации тегов
    делается одним хопом, поэтому неразрешённая цепочка молча дала бы
    промежуточный тег. Циклы разрываются в no-op."""
    resolved: dict[str, str] = {}
    for variant, main_tag in main_of.items():
        seen = [variant]
        target = main_tag
        while target in main_of and target not in seen:
            seen.append(target)
            target = main_of[target]
        if target in main_of:
            log.warning("%s: alias cycle (%s), leaving %r as-is", path, " -> ".join(seen), variant)
            continue
        if len(seen) > 1:
            log.warning(
                "%s: alias chain (%s), resolving %r directly to %r",
                path,
                " -> ".join(seen + [target]),
                variant,
                target,
            )
        resolved[variant] = target
    return resolved


def fingerprint(banned: frozenset[str], aliases: dict[str, str]) -> str:
    """Отпечаток обоих списков для config_hash: правка файлов должна
    форсировать офлайн-пересчёт жанров всей библиотеки."""
    payload = json.dumps(
        {"banned": sorted(banned), "aliases": sorted(aliases.items())},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def migrate_json_if_needed(banlist_path: str, aliases_path: str) -> None:
    """Разовая конвертация со старого формата: genre_banlist.json (JSON-массив)
    и genre_aliases.json (главный тег -> список вариантов, либо ещё более старый
    плоский вариант -> главный тег) превращаются в соседние .txt.

    Исходные JSON остаются на диске нетронутыми — на случай, если конвертация
    не понравится. Повторно не срабатывает: как только .txt существует, он и
    считается источником правды.
    """
    _migrate_banlist(banlist_path)
    _migrate_aliases(aliases_path)


def _legacy_path(path: str, legacy_name: str) -> Optional[str]:
    legacy = Path(path).with_name(legacy_name)
    return str(legacy) if legacy.exists() else None


def _migrate_banlist(path: str) -> None:
    if os.path.exists(path):
        return
    legacy = _legacy_path(path, "genre_banlist.json")
    if not legacy:
        return
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot migrate %s: %s", legacy, exc)
        return
    if not isinstance(raw, list):
        log.warning("Cannot migrate %s: expected a JSON array", legacy)
        return
    tags = sorted({c for item in raw if (c := canonicalize_tag_name(str(item)))})
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Жанры, которые никогда не попадают в ID3. По одному на строку.\n")
        f.write(f"# Сконвертировано из {Path(legacy).name}, исходный файл оставлен как есть.\n\n")
        for tag in tags:
            f.write(f"{tag}\n")
    log.info("Migrated %d banned genre(s) from %s to %s", len(tags), legacy, path)


def _migrate_aliases(path: str) -> None:
    if os.path.exists(path):
        return
    legacy = _legacy_path(path, "genre_aliases.json")
    if not legacy:
        return
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot migrate %s: %s", legacy, exc)
        return
    if not isinstance(raw, dict):
        log.warning("Cannot migrate %s: expected a JSON object", legacy)
        return

    groups: dict[str, set[str]] = {}
    for key, value in raw.items():
        key_canonical = canonicalize_tag_name(str(key))
        if not key_canonical:
            continue
        if isinstance(value, list):
            main_tag, variants = key_canonical, [canonicalize_tag_name(str(v)) for v in value]
        else:
            # Ещё более старый плоский формат: ключ — вариант, значение — главный тег.
            main_tag, variants = canonicalize_tag_name(str(value)), [key_canonical]
        if not main_tag:
            continue
        for variant in variants:
            if variant and variant != main_tag:
                groups.setdefault(main_tag, set()).add(variant)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Синонимы: 'главный тег <- вариант1, вариант2'.\n")
        f.write(f"# Сконвертировано из {Path(legacy).name}, исходный файл оставлен как есть.\n\n")
        for main_tag in sorted(groups):
            f.write(f"{main_tag} {ALIAS_SEPARATOR} {', '.join(sorted(groups[main_tag]))}\n")
    log.info("Migrated %d alias group(s) from %s to %s", len(groups), legacy, path)
