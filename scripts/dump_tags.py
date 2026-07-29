#!/usr/bin/env python3
"""Аудит жанров, уже накопленных в кэше genre_tagger.

Не обращается к сети — берёт уже сохранённые в кэше данные каждого артиста и
печатает:
  1. жанры, реально стоящие в ID3, по числу ТРЕКОВ (не артистов) —
     главная метрика "мусорности в вашей библиотеке";
  2. отсортированный список сырых канонических тегов Last.fm с суммарным
     весом (весь пул тегов, не только принятые в ID3);
  3. кандидатов на объединение — теги, похожие по написанию (difflib), которые
     мог не заметить обычный просмотр списка (например 'ska punk' / 'skacore').

Использование:
    python3 scripts/dump_tags.py --db-path ./data/genres.db --max-tracks 10
    python3 scripts/dump_tags.py --db-path ./data/genres.db --ban-below 10
"""

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cache import Cache  # noqa: E402
from src.lastfm import (  # noqa: E402
    TAG_BLOCKLIST,
    YEAR_RE,
    canonicalize_tag_name,
    load_banlist,
    save_banlist,
)


def aggregate(cache: Cache) -> dict[str, int]:
    """Суммарный вес каждого канонического тега по ВСЕМ тегам Last.fm (не
    только тем 1-3, что реально попали в ID3) — полезно, чтобы найти кандидатов
    на --add-alias (похожее написание) до того, как они вообще будут приняты.
    Помни: count в ответе Last.fm — это вес ОТНОСИТЕЛЬНО топ-тега артиста
    (у топ-тега всегда 100), а не абсолютное число людей, поставивших тег — так
    что большой totals[tag] может быть за счёт одного артиста с высоким весом,
    а не за счёт того, что тег реально распространён в библиотеке."""
    totals: dict[str, int] = {}
    for _artist, raw_json in cache.iter_with_raw_response():
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        for tag in tags:
            canonical = canonicalize_tag_name(tag.get("name", ""))
            if not canonical or canonical in TAG_BLOCKLIST or YEAR_RE.match(canonical):
                continue
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            totals[canonical] = totals.get(canonical, 0) + count
    return totals


def final_genre_track_counts(cache: Cache) -> dict[str, int]:
    """На скольких треках (файлах) реально стоит каждый жанр после применения
    MIN_TAG_COUNT/MAX_GENRES/алиасов/бан-листа (т.е. то, что реально ушло в
    ID3). Это метрика "мусорности в вашей библиотеке" в треках, а не в
    артистах: у артиста с одним синглом и артиста с полной дискографией из
    200 треков жанр весит по-разному. Сортируй по возрастанию, чтобы
    кандидаты на --ban-genre/--add-alias были сверху."""
    counts: dict[str, int] = {}
    for _artist, genres, track_count in cache.iter_genre_track_counts():
        if not genres:
            continue
        for genre in set(genres):
            counts[genre] = counts.get(genre, 0) + track_count
    return counts


def final_genre_artist_counts(cache: Cache) -> dict[str, int]:
    """На скольких РАЗНЫХ артистах реально стоит каждый жанр — вспомогательная
    метрика рядом с final_genre_track_counts(), показывается для контекста
    (жанр на 1 треке у 1 артиста vs жанр на 1 треке у 10 артистов с синглами —
    разная ситуация)."""
    counts: dict[str, int] = {}
    for _artist, genres in cache.iter_genres():
        if not genres:
            continue
        for genre in set(genres):
            counts[genre] = counts.get(genre, 0) + 1
    return counts


def find_similar_clusters(tags: list[str], cutoff: float) -> list[list[str]]:
    """Группирует канонические теги, похожие по написанию (не по смыслу!).
    Жадный алгоритм: не гарантирует глобально оптимальную кластеризацию, но
    для ручного просмотра кандидатов на объединение этого достаточно."""
    remaining = set(tags)
    clusters = []
    for tag in sorted(tags):
        if tag not in remaining:
            continue
        remaining.discard(tag)
        matches = difflib.get_close_matches(tag, remaining, n=10, cutoff=cutoff)
        if matches:
            remaining.difference_update(matches)
            clusters.append([tag] + matches)
    return clusters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default=os.environ.get("DB_PATH", "/data/genres.db"))
    parser.add_argument(
        "--cutoff",
        type=float,
        default=0.8,
        help="Порог схожести строк для кластеризации, 0..1 (difflib.SequenceMatcher.ratio, по умолчанию 0.8)",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="Не показывать теги с суммарным count по всей библиотеке меньше этого значения (влияет только на секцию сырых тегов и кластеры)",
    )
    parser.add_argument(
        "--max-tracks",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Показать только жанры, реально стоящие (после MIN_TAG_COUNT/MAX_GENRES/"
            "алиасов/бан-листа) не более чем на N треках — быстрый способ найти "
            "кандидатов на --ban-genre/--add-alias (например --max-tracks 10)"
        ),
    )
    parser.add_argument(
        "--ban-below",
        type=int,
        metavar="N",
        help=(
            "Не только показать, но и сразу дописать в GENRE_BANLIST_FILE все "
            "жанры с числом треков <= N. Ничего не сканирует библиотеку сам по "
            "себе — эффект применится на следующем обычном --once, как обычно "
            "для бан-листа. Путь к файлу берётся из --banlist-path."
        ),
    )
    parser.add_argument(
        "--banlist-path",
        default=os.environ.get("GENRE_BANLIST_FILE", "/data/genre_banlist.json"),
        help="Путь к файлу бан-листа для --ban-below/фильтрации уже забаненных (по умолчанию $GENRE_BANLIST_FILE)",
    )
    parser.add_argument(
        "--suggest-bans-file",
        metavar="PATH",
        help=(
            "Вместо (или вместе с) немедленного --ban-below записать кандидатов "
            "в текстовый файл для ручного review: по одному жанру на строку, с "
            "комментарием '# N track(s), M artist(s)'. Удалите строки с "
            "жанрами, которые хотите оставить, затем примените отредактированный "
            "список одной командой: "
            "docker compose run --rm genre-tagger --ban-genre-file PATH. "
            "Уже присутствующие в бан-листе жанры в файл не попадают (не "
            "предлагаются повторно)."
        ),
    )
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"База {args.db_path!r} не найдена", file=sys.stderr)
        sys.exit(1)

    cache = Cache(args.db_path)
    try:
        totals = aggregate(cache)
        track_counts = final_genre_track_counts(cache)
        artist_counts = final_genre_artist_counts(cache)
    finally:
        cache.close()

    already_banned = load_banlist(args.banlist_path)

    print(f"=== {len(track_counts)} уникальных жанров реально в ID3 (по трекам) ===")
    print("(сортировка по возрастанию — кандидаты на --ban-genre/--add-alias сверху)\n")
    shown_final = {
        tag: count
        for tag, count in track_counts.items()
        if tag not in already_banned and (args.max_tracks is None or count <= args.max_tracks)
    }
    if not shown_final:
        print("(пусто — либо кэш пуст, либо ни один жанр не подходит под --max-tracks)")
    else:
        for tag, count in sorted(shown_final.items(), key=lambda item: (item[1], item[0])):
            print(f"{count:>6} track(s) ({artist_counts.get(tag, 0)} artist(s))  {tag}")

    if args.suggest_bans_file:
        with open(args.suggest_bans_file, "w", encoding="utf-8") as f:
            f.write(
                "# Кандидаты на бан жанров. Удалите строки с жанрами, которые хотите\n"
                "# оставить, затем примените отредактированный список:\n"
                f"#   docker compose run --rm genre-tagger --ban-genre-file {args.suggest_bans_file}\n\n"
            )
            for tag, count in sorted(shown_final.items(), key=lambda item: (item[1], item[0])):
                f.write(f"{tag}  # {count} track(s), {artist_counts.get(tag, 0)} artist(s)\n")
        print(
            f"\n=== Записано {len(shown_final)} кандидат(ов) в {args.suggest_bans_file} "
            "для ручного review ==="
        )

    if args.ban_below is not None:
        to_ban = {tag for tag, count in track_counts.items() if count <= args.ban_below}
        if to_ban:
            existing = set(load_banlist(args.banlist_path))
            save_banlist(args.banlist_path, frozenset(existing | to_ban))
            print(
                f"\n=== Добавлено {len(to_ban)} жанр(ов) в бан-лист {args.banlist_path} "
                f"(порог <= {args.ban_below} треков) ==="
            )
            for tag in sorted(to_ban):
                print(f"  {tag}")
            print(
                "\nЭффект применится на следующем обычном запуске (--once): жанр "
                "снимется с уже затегированных треков и из БД, без обращения к Last.fm."
            )
        else:
            print(f"\nНи один жанр не попадает под порог <= {args.ban_below} треков, бан-лист не менялся.")

    totals = {tag: count for tag, count in totals.items() if count >= args.min_count}
    if totals:
        print(f"\n=== {len(totals)} уникальных канонических тегов Last.fm (сырой вес, все теги) ===\n")
        for tag, count in sorted(totals.items()):
            print(f"{count:>8}  {tag}")

        clusters = [c for c in find_similar_clusters(list(totals.keys()), args.cutoff) if len(c) > 1]
        if clusters:
            print(f"\n=== {len(clusters)} кандидат(ов) на объединение (похожее написание, cutoff={args.cutoff}) ===\n")
            for cluster in clusters:
                names = ", ".join(f"{tag!r} ({totals[tag]})" for tag in cluster)
                print(f"  - {names}")
        else:
            print("\nПохожих по написанию тегов не найдено.")


if __name__ == "__main__":
    main()
