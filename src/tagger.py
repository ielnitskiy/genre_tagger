import logging

from mutagen import MutagenError
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3NoHeaderError

log = logging.getLogger(__name__)


def has_genre(path: str) -> bool:
    """Дешёвая локальная проверка без сети. Возвращает True и при ошибке
    чтения (битый/недописанный файл) — вызывающий код должен сам решить,
    ретраить ли такой файл (см. write_genre и scanner.py)."""
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        return False
    except (MutagenError, OSError) as exc:
        log.warning("Cannot read ID3 tags from %s, skipping this pass: %s", path, exc)
        raise
    return bool(tags.get("genre"))


def write_genre(path: str, genres: list[str], force: bool = False) -> None:
    """Пишет жанр(ы) в TCON. Если force=False и тег уже есть — не перезаписывает
    (safety-belt). force=True используется для --reset-artist и пересчёта
    фильтрации из raw_response, где перезапись как раз нужна."""
    try:
        if not force and has_genre(path):
            return
    except (MutagenError, OSError):
        return

    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = EasyID3()
    tags["genre"] = genres
    tags.save(path, v2_version=4)
