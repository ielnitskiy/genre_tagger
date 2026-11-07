import logging

import mutagen
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

log = logging.getLogger(__name__)

# Форматы, в которые пишется жанр. В mp3 — тег ID3 (TCON), во FLAC — Vorbis
# comment GENRE, в m4a — атом ©gen; mutagen в режиме easy даёт им общий ключ.
AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a")


def is_audio_file(filename: str) -> bool:
    return filename.lower().endswith(AUDIO_EXTENSIONS)


def _is_mp3(path: str) -> bool:
    return path.lower().endswith(".mp3")


def _open(path: str, create: bool):
    """Теги файла в режиме easy. Для mp3 без ID3-заголовка при create=True
    создаётся пустой тег, при create=False возвращается None. Ошибки чтения
    (MutagenError/OSError) пробрасываются."""
    if _is_mp3(path):
        try:
            return EasyID3(path)
        except ID3NoHeaderError:
            return EasyID3() if create else None
    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise MutagenError(f"unsupported or unreadable audio file: {path}")
    if audio.tags is None:
        if not create:
            return None
        audio.add_tags()
    return audio


def _save(tags, path: str) -> None:
    if _is_mp3(path):
        tags.save(path, v2_version=4)
    else:
        tags.save()


def has_genre(path: str) -> bool:
    """Дешёвая локальная проверка без сети. Ошибку чтения (битый/недописанный
    файл) пробрасывает — вызывающий код сам решает, ретраить ли такой файл."""
    try:
        tags = _open(path, create=False)
    except (MutagenError, OSError) as exc:
        log.warning("Cannot read tags from %s, skipping this pass: %s", path, exc)
        raise
    return bool(tags is not None and tags.get("genre"))


def read_genres(path: str) -> list[str]:
    """Жанры, уже стоящие в файле. Нечитаемый файл — пустой список."""
    try:
        tags = _open(path, create=False)
    except (MutagenError, OSError):
        return []
    return list(tags.get("genre", [])) if tags is not None else []


def write_genre(path: str, genres: list[str], force: bool = False) -> None:
    """Пишет жанр(ы). Если force=False и жанр уже есть — не перезаписывает.

    Ошибки чтения/записи (MutagenError/OSError) намеренно не перехватываются
    здесь — пробрасываются вызывающему коду (scanner._tag_new_files), чтобы файл
    попал в failed_files и был повторён на следующем скане, а не пропущен молча."""
    if not force and has_genre(path):
        return
    tags = _open(path, create=True)
    tags["genre"] = genres
    _save(tags, path)


def remove_genre(path: str) -> bool:
    """Удаляет жанр, если он есть. Возвращает True, если файл был изменён.
    Ошибки (MutagenError/OSError) пробрасываются вызывающему коду."""
    tags = _open(path, create=False)
    if tags is None or "genre" not in tags:
        return False
    del tags["genre"]
    _save(tags, path)
    return True
