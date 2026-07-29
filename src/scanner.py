import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mutagen import MutagenError

from . import tagger
from .cache import Cache
from .config import Config
from .lastfm import LastfmClient

log = logging.getLogger(__name__)

# Гарантированно не совпадает с реальным mtime папки — используется, чтобы
# форсировать повторный listdir альбома на следующем скане, если в этом
# проходе не удалось протегировать какой-то файл (см. _tag_new_files).
FORCE_RECHECK_MTIME = -1.0


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


def _list_mp3_files(album_path: str) -> set[str]:
    files = set()
    for root, _dirs, filenames in os.walk(album_path):
        for filename in filenames:
            if filename.lower().endswith(".mp3"):
                full = os.path.join(root, filename)
                files.add(os.path.relpath(full, album_path))
    return files


def _tag_new_files(
    artist_path: str,
    album_name: str,
    new_files_rel: set[str],
    genres: list[str],
    force: bool,
    failed_files: set[str],
) -> None:
    album_path = os.path.join(artist_path, album_name)
    tagged = 0
    for rel_path in new_files_rel:
        full_path = os.path.join(album_path, rel_path)
        try:
            tagger.write_genre(full_path, genres, force=force)
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


def scan_artist(
    artist_name: str,
    music_dir: str,
    cache: Cache,
    lastfm: LastfmClient,
    force_scan: bool = False,
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
        files_in_album = _list_mp3_files(album_path)
        current_albums[album_name] = {"mtime": mtime, "files": sorted(files_in_album)}
        if was_done:
            if force:
                new_files_by_album[album_name] = set(files_in_album)
            else:
                previously_known = set((cached or {}).get("files", []))
                new_files_by_album[album_name] = files_in_album - previously_known

    if was_done:
        if not changed:
            return
    else:
        log.info("New artist %r, resolving genres via Last.fm", artist_name)
        genres, raw_json = lastfm.resolve_genres(artist_name)
        new_files_by_album = {
            album_name: set(data["files"]) for album_name, data in current_albums.items()
        }
        timestamp = datetime.now(timezone.utc).isoformat()
        cache.mark_done(artist_name, genres, current_albums, raw_json, timestamp)

    if genres is not None:
        for album_name, new_files_rel in new_files_by_album.items():
            if not new_files_rel:
                continue
            failed_files: set[str] = set()
            _tag_new_files(artist_path, album_name, new_files_rel, genres, force, failed_files)
            if failed_files:
                # Форсируем повторный listdir этого альбома на следующем скане,
                # независимо от того, изменится ли его mtime на диске.
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
) -> None:
    artist_names = _list_artist_dirs(config.music_dir, config.skip_dirs)
    log.info("Starting scan pass over %d artist dir(s)", len(artist_names))
    for artist_name in artist_names:
        try:
            scan_artist(artist_name, config.music_dir, cache, lastfm, force_scan=force_scan)
        except Exception:
            log.exception("Unexpected error while scanning artist %r, continuing", artist_name)
    log.info("Scan pass finished")
