#!/usr/bin/env python3
"""Аудит тегов Last.fm, уже накопленных в кэше genre_tagger.

Не обращается к сети — берёт полный сырой ответ Last.fm (все теги, не только
те 1-3, что попали в ID3) из artist_genre.raw_response для каждого артиста,
агрегирует по канонической форме (см. LastfmClient._canonicalize) по всей
библиотеке и печатает:
  1. отсортированный список канонических тегов с суммарным count;
  2. кандидатов на объединение — теги, похожие по написанию (difflib), которые
     мог не заметить обычный просмотр списка (например 'ska punk' / 'skacore').

Использование:
    python3 scripts/dump_tags.py --db-path ./data/genres.db
"""

import argparse
import difflib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cache import Cache  # noqa: E402
from src.lastfm import TAG_BLOCKLIST, YEAR_RE, canonicalize_tag_name  # noqa: E402


def aggregate(cache: Cache) -> dict[str, int]:
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
        help="Не показывать теги с суммарным count по всей библиотеке меньше этого значения",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"База {args.db_path!r} не найдена", file=sys.stderr)
        sys.exit(1)

    cache = Cache(args.db_path)
    try:
        totals = aggregate(cache)
    finally:
        cache.close()

    totals = {tag: count for tag, count in totals.items() if count >= args.min_count}
    if not totals:
        print("Тегов не найдено (пустой кэш или все отфильтрованы --min-count).")
        return

    print(f"=== {len(totals)} уникальных канонических тегов ===\n")
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
