import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

API_URL = "http://ws.audioscrobbler.com/2.0/"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MIN_SECONDS_BETWEEN_REQUESTS = 1 / 5  # <=5 запросов/сек
RATE_LIMIT_BACKOFF_SECONDS = 5.0  # используется, если Last.fm не прислал Retry-After

TAG_BLOCKLIST = frozenset(
    {
        "seen live",
        "favorite",
        "favorites",
        "favourite",
        "favourites",
        "love",
        "loved",
        "spotify",
        "male vocalists",
        "female vocalists",
        "male vocalist",
        "female vocalist",
        "awesome",
        "amazing",
        "beautiful",
        "check out",
        "to listen",
        "own it",
    }
)
YEAR_RE = re.compile(r"^\d{2,4}s?$")
# Last.fm отдаёт один и тот же жанр в разном написании ("ska-punk", "ska punk",
# "Ska_Punk") как отдельные теги от разных пользователей — схлопываем дефисы/
# подчёркивания/пробелы и регистр в один канонический вид, чтобы не получить
# дубли в итоговом списке жанров.
SEPARATOR_RE = re.compile(r"[\s_-]+")


def canonicalize_tag_name(name: str) -> str:
    return SEPARATOR_RE.sub(" ", name.strip().lower()).strip()


def load_aliases(path: Optional[str]) -> dict[str, str]:
    """Читает словарь синонимов жанров (см. --add-alias/--list-aliases в main.py).
    Отсутствующий файл — не ошибка, просто словарь ещё пуст. Ключи и значения
    приводятся к тому же каноническому виду, что и теги Last.fm, чтобы словарь
    работал независимо от того, как их записали руками."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot load genre aliases from %s: %s", path, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("Genre aliases file %s must contain a JSON object, ignoring", path)
        return {}
    aliases = {}
    for key, value in raw.items():
        canonical_key = canonicalize_tag_name(str(key))
        canonical_value = canonicalize_tag_name(str(value))
        if canonical_key and canonical_value:
            aliases[canonical_key] = canonical_value
    return aliases


def save_aliases(path: str, aliases: dict[str, str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(aliases.items())), f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_banlist(path: Optional[str]) -> frozenset[str]:
    """Читает список запрещённых жанров (см. --ban-genre/--list-banned-genres
    в main.py). Отсутствующий файл — не ошибка, просто список ещё пуст."""
    if not path or not os.path.exists(path):
        return frozenset()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cannot load genre banlist from %s: %s", path, exc)
        return frozenset()
    if not isinstance(raw, list):
        log.warning("Genre banlist file %s must contain a JSON array, ignoring", path)
        return frozenset()
    return frozenset(
        canonical for item in raw if (canonical := canonicalize_tag_name(str(item)))
    )


def save_banlist(path: str, banned: frozenset[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sorted(banned), f, ensure_ascii=False, indent=2)
        f.write("\n")


class LastfmClient:
    def __init__(
        self,
        api_key: str,
        min_tag_count: int,
        max_genres: int,
        aliases: Optional[dict[str, str]] = None,
        banned: Optional[frozenset[str]] = None,
    ):
        self._api_key = api_key
        self._min_tag_count = min_tag_count
        self._max_genres = max_genres
        self._aliases = aliases or {}
        self._banned = banned or frozenset()
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = MIN_SECONDS_BETWEEN_REQUESTS - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _fetch_top_tags(self, artist: str) -> Optional[str]:
        params = {
            "method": "artist.getTopTags",
            "artist": artist,
            "api_key": self._api_key,
            "format": "json",
            "autocorrect": 1,
        }
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            log.debug("Requesting Last.fm artist.getTopTags for %r (attempt %d)", artist, attempt)
            try:
                resp = requests.get(API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                log.warning("Last.fm request failed for %r (attempt %d): %s", artist, attempt, exc)
                continue
            if resp.status_code == 429:
                wait = self._retry_after_seconds(resp)
                log.warning(
                    "Last.fm 429 (rate limited) for %r (attempt %d), waiting %.1fs",
                    artist,
                    attempt,
                    wait,
                )
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                log.warning("Last.fm 5xx for %r (attempt %d): %s", artist, attempt, resp.status_code)
                continue
            log.debug("Last.fm responded %d for %r", resp.status_code, artist)
            return resp.text
        log.warning("Giving up on Last.fm request for %r after %d attempts", artist, MAX_RETRIES + 1)
        return None

    @staticmethod
    def _retry_after_seconds(resp: requests.Response) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass
        return RATE_LIMIT_BACKOFF_SECONDS

    def resolve_genres(self, artist: str) -> tuple[Optional[list[str]], Optional[str]]:
        """Возвращает (жанры или None, сырой JSON-ответ или None)."""
        raw = self._fetch_top_tags(artist)
        if raw is None:
            return None, None
        genres = self.parse_tags(raw)
        if genres is None:
            log.info("No usable genres from Last.fm for %r", artist)
        else:
            log.info("Resolved genres for %r: %s", artist, genres)
        return genres, raw

    def parse_tags(self, raw_json: str) -> Optional[list[str]]:
        """Разбирает уже полученный ответ Last.fm без обращения к сети —
        используется и для свежего запроса, и для пересчёта из raw_response
        при смене MIN_TAG_COUNT/MAX_GENRES."""
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return None
        if "error" in data:
            return None
        tags = data.get("toptags", {}).get("tag", [])
        if isinstance(tags, dict):
            tags = [tags]
        return self._filter_tags(tags)

    def _filter_tags(self, tags: list[dict]) -> Optional[list[str]]:
        # Дубли-варианты одного жанра (регистр, дефис/пробел/подчёркивание)
        # схлопываются в canonical-ключ, их count суммируется — иначе один и тот
        # же жанр, размеченный по-разному разными пользователями Last.fm, мог бы
        # попасть в теги дважды или не набрать MIN_TAG_COUNT по отдельности.
        merged_counts: dict[str, int] = {}
        for tag in tags:
            name = tag.get("name", "")
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            canonical = canonicalize_tag_name(name)
            if not canonical:
                continue
            canonical = self._aliases.get(canonical, canonical)
            if canonical in TAG_BLOCKLIST or canonical in self._banned:
                continue
            if YEAR_RE.match(canonical):
                continue
            merged_counts[canonical] = merged_counts.get(canonical, 0) + count

        filtered = [
            (count, name) for name, count in merged_counts.items() if count >= self._min_tag_count
        ]
        if not filtered:
            return None
        # Не полагаемся на то, что Last.fm сам вернул теги отсортированными по
        # весу — сортируем явно, чтобы max_genres гарантированно были самыми
        # "жирными" тегами, а не первыми N в порядке ответа API. При равном
        # весе порядок иначе зависел бы от порядка ответа API (недетерминированно
        # для пользователя) — разрешаем по алфавиту.
        filtered.sort(key=lambda item: (-item[0], item[1]))
        return [name for _count, name in filtered[: self._max_genres]]
