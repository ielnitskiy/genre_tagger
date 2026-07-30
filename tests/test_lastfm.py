import json

import pytest
import requests
import responses

from src.lastfm import API_URL, MAX_GENRES, LastfmClient


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Троттлинг и backoff используют time.sleep — тесты не должны реально ждать."""
    monkeypatch.setattr("src.lastfm.time.sleep", lambda _seconds: None)


def _client(aliases=None, banned=None):
    return LastfmClient(api_key="testkey", aliases=aliases, banned=banned)


def _toptags_body(tags):
    return json.dumps({"toptags": {"tag": tags}})


@responses.activate
def test_resolve_genres_sorts_by_weight():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "rock", "count": "50"},
                {"name": "indie", "count": "80"},
            ]
        ),
        status=200,
    )
    client = _client()
    genres, raw = client.resolve_genres("Some Artist")
    assert genres == ["indie", "rock"]
    assert raw is not None


@responses.activate
def test_resolve_genres_takes_top_three_and_nothing_more():
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
    client = _client()
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["a", "b", "c"]
    assert len(genres) == MAX_GENRES


@responses.activate
def test_weak_tags_are_kept_when_the_artist_has_nothing_stronger():
    """Порога веса больше нет: если у артиста всего один слабый тег, это всё
    равно лучше, чем отсутствие жанра."""
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "obscure", "count": "1"}]),
        status=200,
    )
    genres, _raw = _client().resolve_genres("Some Artist")
    assert genres == ["obscure"]


@responses.activate
def test_banned_tag_does_not_hand_its_slot_to_the_tail():
    """Ключевое свойство конвейера: топ-3 выбирается один раз, и забаненный тег
    не поднимает наверх четвёртый тег из хвоста."""
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "rock", "count": "100"},
                {"name": "russian", "count": "90"},
                {"name": "punk", "count": "80"},
                {"name": "garage rock", "count": "70"},
            ]
        ),
        status=200,
    )
    client = _client(banned=frozenset({"russian"}))
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock", "punk"]


@responses.activate
def test_two_tags_from_the_top_collapsing_into_one_alias_leave_one_genre():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "hiphop", "count": "100"},
                {"name": "hip-hop", "count": "90"},
            ]
        ),
        status=200,
    )
    client = _client(aliases={"hiphop": "hip hop"})
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["hip hop"]


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
    client = _client()
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["alpha", "zeta"]


def test_canonicalization_is_consistent_across_different_artists():
    """Регрессия на реальный кейс: артисту A Last.fm может отдать 'ska-punk',
    а артисту B — 'ska punk' (разные пользователи Last.fm по-разному
    расставляют теги). Нормализация — чистая функция от строки тега, не от
    артиста, так что оба должны получить одинаковый жанр в ID3."""
    client = _client()
    raw_artist_a = _toptags_body([{"name": "ska-punk", "count": "50"}])
    raw_artist_b = _toptags_body([{"name": "ska punk", "count": "50"}])

    genres_a = client.parse_tags(raw_artist_a)
    genres_b = client.parse_tags(raw_artist_b)

    assert genres_a == genres_b == ["ska punk"]


@responses.activate
def test_resolve_genres_merges_hyphen_space_underscore_variants_of_same_tag():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "ska-punk", "count": "30"},
                {"name": "ska punk", "count": "20"},
                {"name": "Ska_Punk", "count": "10"},
            ]
        ),
        status=200,
    )
    client = _client()
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["ska punk"]  # три варианта одного тега схлопнулись в один


@responses.activate
def test_resolve_genres_merges_case_variants_of_same_tag():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "Rock", "count": "40"},
                {"name": "rock", "count": "40"},
            ]
        ),
        status=200,
    )
    client = _client()
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]


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
    client = _client()
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
    client = _client()
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
    client = _client()
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
    client = _client()
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
    client = _client()
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
    client = _client()
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]


def test_parse_tags_used_for_offline_recompute_without_network():
    client = _client()
    raw = _toptags_body([{"name": "rock", "count": "50"}, {"name": "indie", "count": "90"}])
    assert client.parse_tags(raw) == ["indie", "rock"]


def test_parse_tags_returns_none_on_invalid_json():
    client = _client()
    assert client.parse_tags("not json") is None


@responses.activate
def test_aliases_remap_tag_after_canonicalization():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "hiphop", "count": "50"}]),
        status=200,
    )
    client = _client(aliases={"hiphop": "hip hop"})
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["hip hop"]


@responses.activate
def test_banned_genre_is_excluded_from_result():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "pop", "count": "50"},
                {"name": "disco", "count": "40"},
            ]
        ),
        status=200,
    )
    client = _client(banned=frozenset({"pop"}))
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["disco"]


@responses.activate
def test_banned_genre_applies_after_alias_remap():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "hiphop", "count": "50"}]),
        status=200,
    )
    client = _client(aliases={"hiphop": "hip hop"}, banned=frozenset({"hip hop"})
    )
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres is None


@responses.activate
def test_alias_does_not_bypass_ban_on_the_source_tag_name():
    """Бан проверяется и ДО подстановки алиаса: иначе алиас на забаненное имя
    молча обходил бы бан. CLI такой конфликт создать не даёт, но файл могли
    поправить руками."""
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "trumpet", "count": "50"}]),
        status=200,
    )
    client = _client(aliases={"trumpet": "jazz"}, banned=frozenset({"trumpet"})
    )
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres is None


@responses.activate
def test_alias_does_not_bypass_builtin_tag_blocklist():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "seen live", "count": "50"}]),
        status=200,
    )
    client = _client(aliases={"seen live": "rock"})
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres is None


@responses.activate
def test_all_genres_banned_returns_none():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "pop", "count": "50"}]),
        status=200,
    )
    client = _client(banned=frozenset({"pop"}))
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres is None


