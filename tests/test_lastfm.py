import json

import pytest
import requests
import responses

from src.lastfm import API_URL, LastfmClient


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Троттлинг и backoff используют time.sleep — тесты не должны реально ждать."""
    monkeypatch.setattr("src.lastfm.time.sleep", lambda _seconds: None)


def _client(min_tag_count=10, max_genres=3):
    return LastfmClient(api_key="testkey", min_tag_count=min_tag_count, max_genres=max_genres)


def _toptags_body(tags):
    return json.dumps({"toptags": {"tag": tags}})


@responses.activate
def test_resolve_genres_filters_by_min_count_and_sorts_by_weight():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "rock", "count": "50"},
                {"name": "indie", "count": "80"},
                {"name": "obscure", "count": "1"},  # ниже min_tag_count, отфильтруется
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3)
    genres, raw = client.resolve_genres("Some Artist")
    assert genres == ["indie", "rock"]
    assert raw is not None


@responses.activate
def test_resolve_genres_respects_max_genres():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "a", "count": "100"},
                {"name": "b", "count": "90"},
                {"name": "c", "count": "80"},
                {"name": "d", "count": "70"},
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=0, max_genres=2)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["a", "b"]


@responses.activate
def test_resolve_genres_ties_broken_alphabetically():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "zeta", "count": "50"},
                {"name": "alpha", "count": "50"},
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=0, max_genres=2)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["alpha", "zeta"]


@responses.activate
def test_resolve_genres_filters_blocklisted_and_year_tags():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "seen live", "count": "100"},
                {"name": "2010s", "count": "100"},
                {"name": "shoegaze", "count": "100"},
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=0, max_genres=5)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["shoegaze"]


@responses.activate
def test_resolve_genres_returns_none_when_no_tags_survive_filtering():
    responses.add(responses.GET, API_URL, body=_toptags_body([]), status=200)
    client = _client()
    genres, raw = client.resolve_genres("Some Artist")
    assert genres is None
    assert raw is not None  # raw ответ всё равно сохраняется для будущего пересчёта


@responses.activate
def test_resolve_genres_handles_single_tag_as_dict_not_list():
    """Last.fm возвращает объект (не список) в toptags.tag, если тег ровно один."""
    body = json.dumps({"toptags": {"tag": {"name": "rock", "count": "50"}}})
    responses.add(responses.GET, API_URL, body=body, status=200)
    client = _client(min_tag_count=10, max_genres=3)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]


@responses.activate
def test_resolve_genres_returns_none_on_api_error_payload():
    responses.add(
        responses.GET,
        API_URL,
        body=json.dumps({"error": 6, "message": "Artist not found"}),
        status=200,
    )
    client = _client()
    genres, raw = client.resolve_genres("Unknown Artist")
    assert genres is None
    assert raw is not None


@responses.activate
def test_retries_on_5xx_then_succeeds():
    responses.add(responses.GET, API_URL, status=503)
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "rock", "count": "50"}]),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]
    assert len(responses.calls) == 2


@responses.activate
def test_retries_on_429_with_retry_after_header_then_succeeds():
    responses.add(responses.GET, API_URL, status=429, headers={"Retry-After": "3"})
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "rock", "count": "50"}]),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]
    assert len(responses.calls) == 2


@responses.activate
def test_429_without_retry_after_uses_default_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.lastfm.time.sleep", lambda seconds: sleeps.append(seconds))
    responses.add(responses.GET, API_URL, status=429)
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "rock", "count": "50"}]),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3)
    client.resolve_genres("Some Artist")
    assert 5.0 in sleeps


@responses.activate
def test_gives_up_after_max_retries_and_returns_none():
    responses.add(responses.GET, API_URL, status=500)
    responses.add(responses.GET, API_URL, status=500)
    responses.add(responses.GET, API_URL, status=500)
    client = _client()
    genres, raw = client.resolve_genres("Some Artist")
    assert genres is None
    assert raw is None
    assert len(responses.calls) == 3  # MAX_RETRIES=2 -> 3 попытки всего


@responses.activate
def test_retries_on_connection_error_then_succeeds():
    responses.add(responses.GET, API_URL, body=requests.exceptions.ConnectionError("boom"))
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "rock", "count": "50"}]),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]


def test_parse_tags_used_for_offline_recompute_without_network():
    client = _client(min_tag_count=60, max_genres=3)
    raw = _toptags_body([{"name": "rock", "count": "50"}, {"name": "indie", "count": "90"}])
    assert client.parse_tags(raw) == ["indie"]


def test_parse_tags_returns_none_on_invalid_json():
    client = _client()
    assert client.parse_tags("not json") is None
