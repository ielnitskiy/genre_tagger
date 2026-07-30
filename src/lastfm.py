import json
import logging
import re
import time
from typing import Optional

import requests

from src.tagnames import canonicalize_tag_name

log = logging.getLogger(__name__)

API_URL = "http://ws.audioscrobbler.com/2.0/"
REQUEST_TIMEOUT_SECONDS = 10
MAX_RETRIES = 2
MIN_SECONDS_BETWEEN_REQUESTS = 1 / 5  # <=5 запросов/сек
RATE_LIMIT_BACKOFF_SECONDS = 5.0  # используется, если Last.fm не прислал Retry-After

# Сколько тегов уходит в ID3. Не настраивается: три жанра — это ровно столько,
# сколько имеет смысл для навигации по библиотеке, а каждая лишняя ручка здесь
# оборачивалась вопросом "почему у меня столько жанров".
MAX_GENRES = 3

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


class LastfmClient:
    def __init__(
        self,
        api_key: str,
        aliases: Optional[dict[str, str]] = None,
        banned: Optional[frozenset[str]] = None,
    ):
        self._api_key = api_key
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
        """Весь конвейер целиком, в одном месте и в одном направлении:

            теги Last.fm -> выбросить служебные -> топ-3 по весу
                         -> бан-лист -> алиасы -> 0..3 жанра

        Каждый шаг только убирает или переименовывает уже выбранное. Ничего не
        поднимается "снизу" взамен выброшенного: сколько тегов пережило
        фильтры, столько и уйдёт в ID3 — хоть три, хоть один, хоть ни одного.
        """
        best_count: dict[str, int] = {}
        for tag in tags:
            canonical = canonicalize_tag_name(tag.get("name", ""))
            if not canonical or canonical in TAG_BLOCKLIST or YEAR_RE.match(canonical):
                continue
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            # Один жанр в разном написании ("ska-punk"/"ska punk") — это один и
            # тот же тег; берём наибольший вес, а не сумму: count у Last.fm это
            # вес относительно топ-тега артиста (у топ-тега всегда 100), и
            # складывать проценты бессмысленно.
            best_count[canonical] = max(best_count.get(canonical, 0), count)

        # Сортируем явно: полагаться на порядок ответа API нельзя, а при равном
        # весе алфавит даёт детерминированный результат.
        ranked = sorted(best_count.items(), key=lambda item: (-item[1], item[0]))

        genres: list[str] = []
        for name, _count in ranked[:MAX_GENRES]:
            if name in self._banned:
                continue
            name = self._aliases.get(name, name)
            # Алиас может указывать на забаненный жанр (файлы правятся руками),
            # а ещё два разных тега из топ-3 могут схлопнуться в один и тот же.
            if name in self._banned or name in genres:
                continue
            genres.append(name)
        return genres or None
