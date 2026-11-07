import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mutagen import MutagenError

from . import tagger
from .cache import Cache
from .config import Config
from .fallback import FallbackResolver
from .lastfm import LastfmClient

log = logging.getLogger(__name__)

# Гарантированно не совпадает с реальным mtime папки — используется, чтобы
# форсировать повторный listdir альбома на следующем скане, если в этом
# проходе не удалось протегировать какой-то файл (см. _tag_new_files).
FORCE_RECHECK_MTIME = -1.0

# Сколько файлов исполнителя читать ради уже стоящего жанра для запасной цепочки.
FALLBACK_FILES_TO_READ = 5


def _list_artist_dirs(music_dir: str, skip_dirs: frozenset[str]) -> list[str]:
    try:
        entries = os.listdir(music_dir)
    except OSError as exc:
        log.error("Cannot list music dir %s: %s", music_dir, exc)
        return []
    artists = []
    for name in sorted(entries):
        if name.startswith(".") or name in skip_dirs:
            continue
        path = os.path.join(music_dir, name)
        if os.path.isdir(path):
            artists.append(name)
    return artists


def _list_album_dirs(artist_path: str) -> list[str]:
    try:
        entries = os.listdir(artist_path)
    except OSError as exc:
        log.error("Cannot list artist dir %s: %s", artist_path, exc)
        return []
    return sorted(
        name for name in entries
        if not name.startswith(".") and os.path.isdir(os.path.join(artist_path, name))
    )


def _list_audio_files(album_path: str) -> set[str]:
    files = set()
    for root, _dirs, filenames in os.walk(album_path):
        for filename in filenames:
            if tagger.is_audio_file(filename):
                full = os.path.join(root, filename)
                files.add(os.path.relpath(full, album_path))
    return files


def _tag_new_files(
    artist_path: str,
    album_name: str,
    new_files_rel: set[str],
    genres: list[str],
    failed_files: set[str],
) -> None:
    album_path = os.path.join(artist_path, album_name)
    tagged = 0
    for rel_path in new_files_rel:
        full_path = os.path.join(album_path, rel_path)
        try:
            # Перезаписываем всегда: жанр в файле мог поставить кто угодно
            # (старые рипы, spotDL), а в библиотеке должен быть только наш.
            tagger.write_genre(full_path, genres, force=True)
            tagged += 1
        except (MutagenError, OSError) as exc:
            log.warning("Failed to tag %s, will retry next scan: %s", full_path, exc)
            failed_files.add(rel_path)
    log.info(
        "Tagged %d/%d file(s) in album %r with genres %s",
        tagged,
        len(new_files_rel),
        album_name,
        genres,
    )


def _strip_genre_from_files(
    artist_path: str,
    album_name: str,
    files_rel: set[str],
    failed_files: set[str],
) -> None:
    album_path = os.path.join(artist_path, album_name)
    stripped = 0
    for rel_path in files_rel:
        full_path = os.path.join(album_path, rel_path)
        try:
            if tagger.remove_genre(full_path):
                stripped += 1
        except (MutagenError, OSError) as exc:
            log.warning("Failed to strip genre from %s, will retry next scan: %s", full_path, exc)
            failed_files.add(rel_path)
    log.info(
        "Stripped genre from %d/%d file(s) in album %r (no genres left after filtering, e.g. banlist)",
        stripped,
        len(files_rel),
        album_name,
    )


def _resolve_fallback(
    fallback: FallbackResolver,
    lastfm: LastfmClient,
    artist_name: str,
    artist_path: str,
    album_dirs: list[str],
) -> tuple[Optional[list[str]], str]:
    """Запасная цепочка (src/fallback.py) — когда Last.fm ничего не дал.
    Возвращает жанры и JSON для кэша."""
    tracks: list[str] = []
    file_genres: list[str] = []
    files_read = 0
    for album_name in album_dirs:
        album_path = os.path.join(artist_path, album_name)
        for rel_path in sorted(_list_audio_files(album_path)):
            if len(tracks) < 2:
                tracks.append(os.path.splitext(os.path.basename(rel_path))[0])
            if files_read < FALLBACK_FILES_TO_READ:
                files_read += 1
                for genre in tagger.read_genres(os.path.join(album_path, rel_path)):
                    if genre not in file_genres:
                        file_genres.append(genre)
    result = fallback.resolve(artist_name, album_dirs, tracks, file_genres)
    return lastfm.filter_tags(result["tags"]), json.dumps(result, ensure_ascii=False)


def scan_artist(
    artist_name: str,
    music_dir: str,
    cache: Cache,
    lastfm: LastfmClient,
    force_scan: bool = False,
    genre_ttl_days: Optional[int] = None,
    fallback: Optional[FallbackResolver] = None,
) -> None:
    artist_path = os.path.join(music_dir, artist_name)
    album_dirs = _list_album_dirs(artist_path)
    if not album_dirs:
        return

    log.debug("Scanning artist %r (%d album dir(s))", artist_name, len(album_dirs))
    was_done = cache.is_done(artist_name)
    # Не завязано на was_done: --reset-artist удаляет строку артиста из
    # artist_genre, но флаг force-rewrite живёт в отдельной таблице и должен
    # сработать на первом же re-tag'е после ресета (см. вопрос 4 в PLAN.md).
    force = cache.is_force_rewrite(artist_name)
    if was_done:
        genres, cached_albums = cache.get(artist_name)
    else:
        genres, cached_albums = None, {}

    # GENRE_TTL_DAYS: раз в ttl_days перепроверяем жанр уже обработанного
    # артиста у Last.fm — вдруг теги там поменялись. Вызывается до цикла по
    # альбомам намеренно: если жанр реально изменился, force=True должен
    # обойти mtime-gate ниже и на этом же проходе перетегировать уже
    # существующие альбомы новым жанром, а не только новые файлы.
    revalidate = was_done and genre_ttl_days is not None and cache.is_stale(artist_name, genre_ttl_days)
    raw_json = None
    if revalidate:
        log.info(
            "Genre cache for %r is older than %d day(s), revalidating via Last.fm",
            artist_name,
            genre_ttl_days,
        )
        new_genres, raw_json = lastfm.resolve_genres(artist_name)
        fallback_json = None
        if new_genres is None and fallback is not None:
            new_genres, fallback_json = _resolve_fallback(
                fallback, lastfm, artist_name, artist_path, album_dirs
            )
        if new_genres != genres:
            log.info("Genres for %r changed: %s -> %s", artist_name, genres, new_genres)
            force = True
        genres = new_genres

    elif was_done and genres is None and fallback is not None and cache.get_fallback(artist_name) is None:
        # Исполнитель обработан до появления запасной цепочки: опрашиваем её
        # один раз и переписываем все его файлы.
        genres, fallback_json = _resolve_fallback(fallback, lastfm, artist_name, artist_path, album_dirs)
        cache.update_genre(artist_name, genres)
        cache.set_fallback(artist_name, fallback_json)
        force = True

    current_albums: dict[str, dict] = {}
    new_files_by_album: dict[str, set[str]] = {}
    changed = False

    for album_name in album_dirs:
        album_path = os.path.join(artist_path, album_name)
        try:
            mtime = os.stat(album_path).st_mtime
        except OSError as exc:
            log.warning("Cannot stat album dir %s: %s", album_path, exc)
            continue

        cached = cached_albums.get(album_name)
        if cached and cached.get("mtime") == mtime and not force and not force_scan:
            current_albums[album_name] = cached
            continue

        # force=True обходит gate намеренно: --reset-artist / пересчёт из
        # raw_response должны переписать теги, даже если ни один альбом
        # физически не менялся с прошлого скана. force_scan=True обходит gate
        # без принудительной перезаписи тегов — просто честный listdir каждого
        # альбома на этом проходе (страховка от вопроса 5: ненадёжный mtime
        # на сетевой ФС мог скрыть от gate реальные изменения).
        changed = True
        files_in_album = _list_audio_files(album_path)
        current_albums[album_name] = {"mtime": mtime, "files": sorted(files_in_album)}
        if was_done:
            if force:
                new_files_by_album[album_name] = set(files_in_album)
            else:
                previously_known = set((cached or {}).get("files", []))
                new_files_by_album[album_name] = files_in_album - previously_known

    if was_done:
        if revalidate:
            # current_albums на этом этапе уже полный и эквивалентен
            # cached_albums там, где реальных изменений на диске не было
            # (см. ветку раннего continue выше) — можно сохранять как есть.
            timestamp = datetime.now(timezone.utc).isoformat()
            cache.mark_done(artist_name, genres, current_albums, raw_json, timestamp)
            cache.set_fallback(artist_name, fallback_json)
        elif not changed:
            return
    else:
        total_files = sum(len(data["files"]) for data in current_albums.values())
        if total_files == 0:
            # Папка(и) альбома есть, но ни одного .mp3 внутри — похоже на
            # незавершённую/неудачную закачку. Не тратим запрос к Last.fm и не
            # кэшируем артиста: без этого он навсегда застревал бы в БД с
            # резолвленным жанром на 0 реальных треков (см. PLAN.md/README про
            # расхождение track_count < artist_count). Каждый следующий скан
            # будет по-прежнему бесплатно перепроверять эту папку — как только
            # реальные файлы появятся, артист обработается как обычно.
            log.info(
                "Artist %r has album dir(s) but no .mp3 files yet (incomplete/failed "
                "download?), skipping without querying Last.fm",
                artist_name,
            )
            return
        log.info("New artist %r, resolving genres via Last.fm", artist_name)
        genres, raw_json = lastfm.resolve_genres(artist_name)
        fallback_json = None
        if genres is None and fallback is not None:
            genres, fallback_json = _resolve_fallback(fallback, lastfm, artist_name, artist_path, album_dirs)
        new_files_by_album = {
            album_name: set(data["files"]) for album_name, data in current_albums.items()
        }
        timestamp = datetime.now(timezone.utc).isoformat()
        cache.mark_done(artist_name, genres, current_albums, raw_json, timestamp)
        cache.set_fallback(artist_name, fallback_json)

    if genres is not None:
        for album_name, new_files_rel in new_files_by_album.items():
            if not new_files_rel:
                continue
            failed_files: set[str] = set()
            _tag_new_files(artist_path, album_name, new_files_rel, genres, failed_files)
            if failed_files:
                # Форсируем повторный listdir этого альбома на следующем скане,
                # независимо от того, изменится ли её mtime на диске.
                current_albums[album_name]["mtime"] = FORCE_RECHECK_MTIME
    else:
        # Жанров у исполнителя нет (Last.fm ничего не дал или всё отфильтровано).
        # Снимаем тег и с новых файлов: в них мог остаться жанр от старого рипа
        # или spotDL, а он хуже, чем никакого.
        for album_name, new_files_rel in new_files_by_album.items():
            if not new_files_rel:
                continue
            failed_files = set()
            _strip_genre_from_files(artist_path, album_name, new_files_rel, failed_files)
            if failed_files:
                current_albums[album_name]["mtime"] = FORCE_RECHECK_MTIME

    if force:
        cache.clear_force_rewrite(artist_name)

    if changed:
        # Не только для was_done: если тегирование только что созданной (или
        # только что сброшенной) записи артиста частично не удалось, нужно
        # переписать current_albums поверх того, что mark_done уже сохранил,
        # иначе FORCE_RECHECK_MTIME из шага выше потеряется.
        cache.update_albums(artist_name, current_albums)


def run_once(
    config: Config,
    cache: Cache,
    lastfm: LastfmClient,
    force_scan: bool = False,
    limit: Optional[int] = None,
    fallback: Optional[FallbackResolver] = None,
) -> None:
    artist_names = _list_artist_dirs(config.music_dir, config.skip_dirs)
    # limit режет только то, что реально обрабатываем в этом проходе;
    # orphaned-проверка ниже должна видеть полный artist_names, иначе
    # артисты за пределами limit ошибочно попадут в "нет папки на диске".
    scan_names = artist_names[:limit] if limit is not None else artist_names
    log.info(
        "Starting scan pass over %d artist dir(s)%s",
        len(scan_names),
        f" (limited to first {limit} of {len(artist_names)})" if limit is not None else "",
    )
    for artist_name in scan_names:
        try:
            scan_artist(
                artist_name,
                config.music_dir,
                cache,
                lastfm,
                force_scan=force_scan,
                genre_ttl_days=config.genre_ttl_days,
                fallback=fallback,
            )
        except Exception:
            log.exception("Unexpected error while scanning artist %r, continuing", artist_name)

    orphaned = set(cache.list_artists()) - set(artist_names) - config.skip_dirs
    for artist_name in sorted(orphaned):
        log.warning(
            "Cached artist %r has no directory under %s anymore (orphaned cache entry, not removed automatically)",
            artist_name,
            config.music_dir,
        )

    log.info("Scan pass finished")


def wipe_all_genre_tags(music_dir: str, dry_run: bool = False) -> tuple[int, int, int]:
    """Обходит весь MUSIC_DIR (не только известные кэшу артисты) и снимает тег
    genre с каждого mp3. dry_run=True ничего не меняет — только считает, у
    скольких файлов сейчас есть genre, чтобы --wipe-all-genres мог показать
    масштаб операции перед подтверждением (см. main.py).
    Возвращает (сколько файлов просмотрено, сколько затронуто/будет затронуто,
    сколько не удалось прочитать)."""
    scanned = affected = failed = 0
    for root, _dirs, filenames in os.walk(music_dir):
        for filename in filenames:
            if not tagger.is_audio_file(filename):
                continue
            full_path = os.path.join(root, filename)
            scanned += 1
            try:
                changed = tagger.has_genre(full_path) if dry_run else tagger.remove_genre(full_path)
            except (MutagenError, OSError) as exc:
                log.warning("Failed to inspect/wipe genre for %s: %s", full_path, exc)
                failed += 1
                continue
            if changed:
                affected += 1
    return scanned, affected, failed
