import json

import pytest
import requests
import responses

from src.lastfm import API_URL, LastfmClient, load_aliases, save_aliases


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Троттлинг и backoff используют time.sleep — тесты не должны реально ждать."""
    monkeypatch.setattr("src.lastfm.time.sleep", lambda _seconds: None)


def _client(min_tag_count=10, max_genres=3, aliases=None):
    return LastfmClient(
        api_key="testkey", min_tag_count=min_tag_count, max_genres=max_genres, aliases=aliases
    )


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


def test_canonicalization_is_consistent_across_different_artists():
    """Регрессия на реальный кейс: артисту A Last.fm может отдать 'ska-punk',
    а артисту B — 'ska punk' (разные пользователи Last.fm по-разному
    расставляют теги). Нормализация — чистая функция от строки тега, не от
    артиста, так что оба должны получить одинаковый жанр в ID3."""
    client = _client(min_tag_count=0, max_genres=5)
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
    client = _client(min_tag_count=10, max_genres=5)
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
    client = _client(min_tag_count=10, max_genres=5)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["rock"]


@responses.activate
def test_resolve_genres_merged_counts_can_clear_min_tag_count_together():
    """Ни один вариант не набирает MIN_TAG_COUNT по отдельности, но суммарно —
    набирают, и жанр не должен быть потерян из-за того, что теги раздроблены
    между разными написаниями одного и того же жанра."""
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "ska-punk", "count": "6"},
                {"name": "ska punk", "count": "6"},
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=5)
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["ska punk"]


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


@responses.activate
def test_aliases_remap_tag_after_canonicalization():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body([{"name": "hiphop", "count": "50"}]),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3, aliases={"hiphop": "hip hop"})
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["hip hop"]


@responses.activate
def test_aliases_merge_counts_of_aliased_and_canonical_forms():
    responses.add(
        responses.GET,
        API_URL,
        body=_toptags_body(
            [
                {"name": "hiphop", "count": "6"},
                {"name": "hip hop", "count": "6"},
            ]
        ),
        status=200,
    )
    client = _client(min_tag_count=10, max_genres=3, aliases={"hiphop": "hip hop"})
    genres, _raw = client.resolve_genres("Some Artist")
    assert genres == ["hip hop"]  # 6 + 6 = 12, суммарно набирает MIN_TAG_COUNT=10


def test_load_aliases_returns_empty_dict_for_missing_file(tmp_path):
    assert load_aliases(str(tmp_path / "missing.json")) == {}


def test_load_aliases_returns_empty_dict_for_none_path():
    assert load_aliases(None) == {}


def test_load_aliases_canonicalizes_keys_and_values(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text('{"HipHop": "Hip-Hop", "dnb": "drum and bass"}', encoding="utf-8")
    assert load_aliases(str(path)) == {"hiphop": "hip hop", "dnb": "drum and bass"}


def test_load_aliases_returns_empty_dict_on_invalid_json(tmp_path, caplog):
    path = tmp_path / "aliases.json"
    path.write_text("not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_aliases(str(path)) == {}
    assert "aliases" in caplog.text.lower()


def test_load_aliases_returns_empty_dict_when_json_is_not_an_object(tmp_path):
    path = tmp_path / "aliases.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_aliases(str(path)) == {}


def test_save_aliases_writes_sorted_json_readable_by_load_aliases(tmp_path):
    path = tmp_path / "nested" / "aliases.json"
    save_aliases(str(path), {"zeta": "z genre", "alpha": "a genre"})

    assert path.exists()
    assert load_aliases(str(path)) == {"zeta": "z genre", "alpha": "a genre"}
    # ключи должны быть отсортированы для читаемых diff'ов при ручном редактировании
    content = path.read_text(encoding="utf-8")
    assert content.index('"alpha"') < content.index('"zeta"')
