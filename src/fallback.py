"""Запасные источники жанра — когда у Last.fm нет тегов исполнителя.

Источники опрашиваются по очереди, берётся первый, чьи теги после общего
фильтра (словарь жанров, алиасы, бан-лист) дали хотя бы один жанр:

    Last.fm (теги альбома и трека) -> MusicBrainz -> Discogs
        -> жанр, уже стоящий в файле -> iTunes

iTunes последний: у него один крупный жанр на исполнителя ("Rock", "Metal").
MusicBrainz, Discogs и iTunes ищут по имени и могут взять однофамильца;
жанры соавторов не используются намеренно — соавтор может играть другое.

Результат хранится в кэше как {"source": ..., "tags": [...]}, чтобы правка
списков пересчитывала жанры без сети.
"""

import json
import logging
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

LASTFM_URL = "http://ws.audioscrobbler.com/2.0/"
MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
DISCOGS_URL = "https://api.discogs.com/database/search"
ITUNES_URL = "https://itunes.apple.com/search"

# MusicBrainz требует осмысленный User-Agent и не больше запроса в секунду;
# Discogs без ключа даёт ~25 запросов в минуту, iTunes — ~20.
USER_AGENT = "genre_tagger/1.0 (personal music library)"
MIN_INTERVAL_SECONDS = {"lastfm": 0.25, "musicbrainz": 1.1, "discogs": 2.5, "itunes": 3.1}
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5.0

MAX_ALBUMS = 2
MAX_TRACKS = 2

FilterTags = Callable[[list[dict]], Optional[list[str]]]


def empty_result() -> dict:
    return {"source": None, "tags": []}


class FallbackResolver:
    def __init__(self, lastfm_api_key: str, filter_tags: FilterTags):
        self._api_key = lastfm_api_key
        self._filter = filter_tags
        self._last_request_at: dict[str, float] = {}

    def resolve(
        self,
        artist: str,
        albums: list[str],
        tracks: list[str],
        file_genres: list[str],
    ) -> dict:
        sources = [
            ("lastfm album/track", lambda: self._lastfm_album_track(artist, albums, tracks)),
            ("musicbrainz", lambda: self._musicbrainz(artist)),
            ("discogs", lambda: self._discogs(artist)),
            ("file", lambda: [{"name": g, "count": 100} for g in file_genres]),
            ("itunes", lambda: self._itunes(artist)),
        ]
        for source, fetch in sources:
            try:
                tags = fetch()
            except (requests.RequestException, ValueError, KeyError) as exc:
                log.warning("Fallback source %s failed for %r: %s", source, artist, exc)
                continue
            if tags and self._filter(tags):
                log.info("Fallback genres for %r from %s: %s", artist, source, self._filter(tags))
                return {"source": source, "tags": tags}
        log.info("No fallback genres for %r", artist)
        return empty_result()

    def _get(self, service: str, url: str, params: dict) -> Optional[dict]:
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL_SECONDS[service] - (
                time.monotonic() - self._last_request_at.get(service, 0.0)
            )
            if wait > 0:
                time.sleep(wait)
            self._last_request_at[service] = time.monotonic()
            resp = requests.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if resp.status_code in (429, 503) and attempt < MAX_RETRIES:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else RETRY_BACKOFF_SECONDS
                except ValueError:
                    delay = RETRY_BACKOFF_SECONDS
                log.debug("%s %d, waiting %.1fs", service, resp.status_code, delay)
                time.sleep(delay * (attempt + 1))
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        return None

    def _lastfm_album_track(self, artist: str, albums: list[str], tracks: list[str]) -> list[dict]:
        tags: list[dict] = []
        requests_to_make = [("album.getTopTags", "album", a) for a in albums[:MAX_ALBUMS]] + [
            ("track.getTopTags", "track", t) for t in tracks[:MAX_TRACKS]
        ]
        for method, field, value in requests_to_make:
            data = self._get(
                "lastfm",
                LASTFM_URL,
                {
                    "method": method,
                    "artist": artist,
                    field: value,
                    "api_key": self._api_key,
                    "format": "json",
                    "autocorrect": 1,
                },
            )
            found = (data or {}).get("toptags", {}).get("tag", [])
            tags.extend([found] if isinstance(found, dict) else found)
        return tags

    def _musicbrainz(self, artist: str) -> list[dict]:
        found = self._get(
            "musicbrainz",
            f"{MUSICBRAINZ_URL}/artist/",
            {"query": f'artist:"{artist}"', "fmt": "json", "limit": 5},
        )
        exact = [
            a for a in (found or {}).get("artists", []) if a.get("name", "").casefold() == artist.casefold()
        ]
        if not exact:
            return []
        details = self._get(
            "musicbrainz",
            f"{MUSICBRAINZ_URL}/artist/{exact[0]['id']}",
            {"inc": "genres", "fmt": "json"},
        )
        return [{"name": g["name"], "count": g.get("count", 0)} for g in (details or {}).get("genres", [])]

    def _discogs(self, artist: str) -> list[dict]:
        found = self._get("discogs", DISCOGS_URL, {"artist": artist, "type": "master", "per_page": 25})
        counts: dict[str, int] = {}
        prefix = artist.casefold() + " - "
        for release in (found or {}).get("results", []):
            # Поиск по артисту нечёткий: берём только релизы, где он указан точно.
            if not release.get("title", "").casefold().startswith(prefix):
                continue
            for name in release.get("style", []) + release.get("genre", []):
                counts[name] = counts.get(name, 0) + 1
        return [{"name": name, "count": count} for name, count in counts.items()]

    def _itunes(self, artist: str) -> list[dict]:
        found = self._get("itunes", ITUNES_URL, {"term": artist, "entity": "musicArtist", "limit": 5})
        for result in (found or {}).get("results", []):
            if result.get("artistName", "").casefold() == artist.casefold() and result.get("primaryGenreName"):
                return [{"name": result["primaryGenreName"], "count": 100}]
        return []


def genres_from_stored(fallback_json: Optional[str], filter_tags: FilterTags) -> Optional[list[str]]:
    """Жанры из сохранённого ответа запасного источника — без сети."""
    if not fallback_json:
        return None
    try:
        stored = json.loads(fallback_json)
    except json.JSONDecodeError:
        return None
    return filter_tags(stored.get("tags") or [])
