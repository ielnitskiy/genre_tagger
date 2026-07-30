"""Канонизация имён тегов — общая и для тегов Last.fm, и для содержимого
banlist.txt/aliases.txt, чтобы списки работали независимо от того, как их
записали руками."""

import re

# Last.fm отдаёт один и тот же жанр в разном написании ("ska-punk", "ska punk",
# "Ska_Punk") как отдельные теги от разных пользователей — схлопываем дефисы/
# подчёркивания/пробелы и регистр в один канонический вид.
SEPARATOR_RE = re.compile(r"[\s_-]+")


def canonicalize_tag_name(name: str) -> str:
    return SEPARATOR_RE.sub(" ", name.strip().lower()).strip()
