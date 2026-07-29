import json
import logging
import re
import time
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


class LastfmClient:
    def __init__(self, api_key: str, min_tag_count: int, max_genres: int):
        self._api_key = api_key
        self._min_tag_count = min_tag_count
        self._max_genres = max_genres
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
            return resp.text
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
        return self.parse_tags(raw), raw

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
        filtered = []
        for tag in tags:
            name = tag.get("name", "")
            try:
                count = int(tag.get("count", 0))
            except (TypeError, ValueError):
                count = 0
            normalized = name.strip().lower()
            if not normalized:
                continue
            if normalized in TAG_BLOCKLIST:
                continue
            if YEAR_RE.match(normalized):
                continue
            if count < self._min_tag_count:
                continue
            filtered.append((count, name))
        if not filtered:
            return None
        # Не полагаемся на то, что Last.fm сам вернул теги отсортированными по
        # весу — сортируем явно, чтобы max_genres гарантированно были самыми
        # "жирными" тегами, а не первыми N в порядке ответа API. При равном
        # весе порядок иначе зависел бы от порядка ответа API (недетерминированно
        # для пользователя) — разрешаем по алфавиту.
        filtered.sort(key=lambda item: (-item[0], item[1].lower()))
        return [name for _count, name in filtered[: self._max_genres]]
