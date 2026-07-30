"""Отчёт по жанрам, которые реально стоят в библиотеке (`--report`).

Заменяет собой прежний scripts/dump_tags.py с десятком флагов. Печатает ровно
две вещи, обе — материал для правки banlist.txt и aliases.txt:

1. жанры по числу треков, редкие внизу — кандидаты в banlist.txt;
2. пары похожих по написанию жанров — кандидаты в aliases.txt.

Сети не касается: считает по тому, что уже лежит в кэше.
"""

import difflib
import logging

from src.cache import Cache

log = logging.getLogger(__name__)

SIMILARITY_CUTOFF = 0.8


def genre_track_counts(cache: Cache) -> dict[str, int]:
    """На скольких треках стоит каждый жанр. Именно треки, а не артисты:
    у артиста с одним синглом и у артиста с дискографией на 200 треков жанр
    весит по-разному."""
    counts: dict[str, int] = {}
    for _artist, genres, track_count in cache.iter_genre_track_counts():
        for genre in set(genres or []):
            counts[genre] = counts.get(genre, 0) + track_count
    return counts


def genre_artist_counts(cache: Cache) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _artist, genres in cache.iter_genres():
        for genre in set(genres or []):
            counts[genre] = counts.get(genre, 0) + 1
    return counts


def similar_pairs(genres: list[str]) -> list[list[str]]:
    """Группы жанров, похожих по НАПИСАНИЮ (не по смыслу): 'hip hop'/'hiphop'.
    Жадная кластеризация — для просмотра глазами этого достаточно."""
    remaining = set(genres)
    clusters = []
    for genre in sorted(genres):
        if genre not in remaining:
            continue
        remaining.discard(genre)
        matches = difflib.get_close_matches(genre, remaining, n=10, cutoff=SIMILARITY_CUTOFF)
        if matches:
            remaining.difference_update(matches)
            clusters.append([genre] + sorted(matches))
    return clusters


def print_report(cache: Cache, banlist_path: str, aliases_path: str) -> None:
    tracks = genre_track_counts(cache)
    artists = genre_artist_counts(cache)

    print(f"=== {len(tracks)} жанр(ов) в библиотеке, по числу треков ===")
    print("Редкие внизу — их обычно и хочется дописать в banlist.txt.\n")
    for genre, count in sorted(tracks.items(), key=lambda item: (-item[1], item[0])):
        print(f"{count:>6} трек(ов)  {artists.get(genre, 0):>4} артист(ов)  {genre}")

    clusters = similar_pairs(list(tracks))
    print(f"\n=== Похожие по написанию: {len(clusters)} групп(ы) ===")
    if not clusters:
        print("(таких нет)")
    else:
        print(f"Строки для {aliases_path} — проверьте, что главный тег слева выбран верно:\n")
        for cluster in clusters:
            main_tag = max(cluster, key=lambda g: (tracks.get(g, 0), g))
            variants = [g for g in cluster if g != main_tag]
            print(f"{main_tag} <- {', '.join(variants)}")

    print(f"\nФайлы: {banlist_path}, {aliases_path}")
    print("Поправьте их и запустите --once: жанры пересчитаются без обращений к Last.fm.")
