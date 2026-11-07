import json

import pytest
import responses

from src import fallback as fb
from src.fallback import FallbackResolver, genres_from_stored


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("src.fallback.time.sleep", lambda _seconds: None)


def keep_known(tags):
    """Фильтр вместо настоящего: пропускает только 'настоящие' жанры."""
    known = {"punk", "metal", "lo-fi", "rock", "emo"}
    names = [t["name"].lower() for t in sorted(tags, key=lambda t: -int(t.get("count", 0)))]
    found = [n for n in names if n in known]
    return found[:3] or None


def resolver():
    return FallbackResolver("key", keep_known)


def mock_lastfm_empty():
    responses.add(responses.GET, fb.LASTFM_URL, json={"toptags": {"tag": []}})


def mock_musicbrainz(name=None, genres=()):
    artists = [{"id": "mbid", "name": name}] if name else []
    responses.add(responses.GET, f"{fb.MUSICBRAINZ_URL}/artist/", json={"artists": artists})
    if name:
        responses.add(
            responses.GET,
            f"{fb.MUSICBRAINZ_URL}/artist/mbid",
            json={"genres": [{"name": g, "count": 1} for g in genres]},
        )


def mock_discogs(results=()):
    responses.add(responses.GET, fb.DISCOGS_URL, json={"results": list(results)})


def mock_itunes(results=()):
    responses.add(responses.GET, fb.ITUNES_URL, json={"results": list(results)})


@responses.activate
def test_lastfm_album_tags_win_when_present():
    responses.add(
        responses.GET,
        fb.LASTFM_URL,
        json={"toptags": {"tag": [{"name": "punk", "count": 100}, {"name": "norway", "count": 90}]}},
    )
    result = resolver().resolve("Band", ["Album"], [], [])
    assert result["source"] == "lastfm album/track"
    assert keep_known(result["tags"]) == ["punk"]


@responses.activate
def test_musicbrainz_needs_an_exact_name_match():
    mock_lastfm_empty()
    mock_musicbrainz(name="Band", genres=["lo-fi"])
    result = resolver().resolve("Band", ["Album"], [], [])
    assert result["source"] == "musicbrainz"


@responses.activate
def test_discogs_counts_only_releases_of_the_exact_artist():
    mock_lastfm_empty()
    mock_musicbrainz()
    mock_discogs(
        [
            {"title": "Band - First", "style": ["Punk"], "genre": ["Rock"]},
            {"title": "Band Of Others - Second", "style": ["Metal"], "genre": ["Rock"]},
        ]
    )
    result = resolver().resolve("Band", ["Album"], [], [])
    assert result["source"] == "discogs"
    assert keep_known(result["tags"]) == ["punk", "rock"]


@responses.activate
def test_file_genre_is_used_before_itunes():
    mock_lastfm_empty()
    mock_musicbrainz()
    mock_discogs()
    result = resolver().resolve("Band", ["Album"], [], ["Emo", "Other"])
    assert result["source"] == "file"
    assert keep_known(result["tags"]) == ["emo"]


@responses.activate
def test_itunes_is_the_last_resort():
    mock_lastfm_empty()
    mock_musicbrainz()
    mock_discogs()
    mock_itunes([{"artistName": "band", "primaryGenreName": "Metal"}])
    result = resolver().resolve("Band", ["Album"], [], [])
    assert result == {"source": "itunes", "tags": [{"name": "Metal", "count": 100}]}


@responses.activate
def test_nothing_found_is_remembered_as_an_empty_result():
    mock_lastfm_empty()
    mock_musicbrainz()
    mock_discogs()
    mock_itunes()
    assert resolver().resolve("Band", ["Album"], [], []) == fb.empty_result()


@responses.activate
def test_a_failing_source_is_skipped():
    mock_lastfm_empty()
    responses.add(responses.GET, f"{fb.MUSICBRAINZ_URL}/artist/", status=500)
    mock_discogs()
    mock_itunes([{"artistName": "Band", "primaryGenreName": "Rock"}])
    assert resolver().resolve("Band", ["Album"], [], [])["source"] == "itunes"


@responses.activate
def test_rate_limit_is_retried():
    mock_lastfm_empty()
    responses.add(responses.GET, f"{fb.MUSICBRAINZ_URL}/artist/", status=503)
    mock_musicbrainz(name="Band", genres=["metal"])
    assert resolver().resolve("Band", ["Album"], [], [])["source"] == "musicbrainz"


def test_genres_from_stored_recomputes_without_network():
    stored = json.dumps({"source": "itunes", "tags": [{"name": "Metal", "count": 100}]})
    assert genres_from_stored(stored, keep_known) == ["metal"]
    assert genres_from_stored(None, keep_known) is None
