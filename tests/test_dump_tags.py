import json

import pytest

from scripts import dump_tags
from src.cache import Cache


def _raw(tags):
    return json.dumps({"toptags": {"tag": [{"name": n, "count": str(c)} for n, c in tags]}})


def _albums(*file_counts):
    """Строит albums-словарь, где i-й альбом содержит file_counts[i] файлов."""
    return {
        f"Album {i}": {"mtime": 0.0, "files": [f"track{n}.mp3" for n in range(count)]}
        for i, count in enumerate(file_counts)
    }


@pytest.fixture
def cache(tmp_path):
    c = Cache(str(tmp_path / "genres.db"))
    yield c
    c.close()


def test_aggregate_sums_counts_across_artists_and_merges_separator_variants(cache):
    cache.mark_done("Artist A", ["ska punk"], {}, _raw([("ska-punk", 40)]), "t")
    cache.mark_done("Artist B", ["ska punk"], {}, _raw([("Ska_Punk", 15)]), "t")

    totals = dump_tags.aggregate(cache)

    assert totals == {"ska punk": 55}


def test_aggregate_skips_blocklisted_and_year_tags(cache):
    cache.mark_done(
        "Artist",
        ["rock"],
        {},
        _raw([("rock", 50), ("seen live", 100), ("2010s", 100)]),
        "t",
    )

    totals = dump_tags.aggregate(cache)

    assert totals == {"rock": 50}


def test_aggregate_ignores_artists_without_raw_response(cache):
    cache.mark_done("Artist", ["rock"], {}, None, "t")
    assert dump_tags.aggregate(cache) == {}


def test_aggregate_skips_unparseable_raw_response(cache):
    cache.mark_done("Artist", None, {}, "not json", "t")
    assert dump_tags.aggregate(cache) == {}


def test_final_genre_track_counts_sums_tracks_not_artists(cache):
    cache.mark_done("Artist A", ["rock", "pop"], _albums(3), None, "t")  # 3 трека
    cache.mark_done("Artist B", ["rock"], _albums(1), None, "t")  # 1 трек
    cache.mark_done("Artist C", ["jazz"], _albums(2, 5), None, "t")  # 7 треков

    counts = dump_tags.final_genre_track_counts(cache)

    assert counts == {"rock": 4, "pop": 3, "jazz": 7}


def test_final_counts_diverge_when_artist_has_genre_but_zero_real_tracks(cache):
    """Регрессия для прод-инцидента, обнаруженного до фикса в
    test_new_artist_with_no_real_files_is_skipped_without_lastfm_call
    (test_scanner.py): такие 0-трековые записи больше не создаются заново, но
    в уже накопленных до фикса данных (или после ручной правки БД) они всё
    ещё могут встречаться — final_genre_track_counts/artist_counts должны
    честно показывать расхождение, а не маскировать его."""
    # 3 живых артиста по 1 треку (3 трека) + 2 с неудачной закачкой (0 треков
    # каждый) = 5 артистов, но только 3 трека — воспроизводит реальный кейс
    # "8 track(s) (10 artist(s)) emoviolence" из прода.
    cache.mark_done("Real Artist 1", ["emoviolence"], _albums(1), None, "t")
    cache.mark_done("Real Artist 2", ["emoviolence"], _albums(1), None, "t")
    cache.mark_done("Real Artist 3", ["emoviolence"], _albums(1), None, "t")
    cache.mark_done("Failed Download Artist 1", ["emoviolence"], _albums(0), None, "t")
    cache.mark_done("Failed Download Artist 2", ["emoviolence"], _albums(0), None, "t")

    track_counts = dump_tags.final_genre_track_counts(cache)
    artist_counts = dump_tags.final_genre_artist_counts(cache)

    assert track_counts["emoviolence"] == 3
    assert artist_counts["emoviolence"] == 5
    assert track_counts["emoviolence"] < artist_counts["emoviolence"]


def test_final_genre_track_counts_ignores_artists_with_no_genre(cache):
    cache.mark_done("Artist A", None, _albums(5), None, "t")
    assert dump_tags.final_genre_track_counts(cache) == {}


def test_final_genre_track_counts_counts_genre_once_per_artist_not_per_occurrence(cache):
    # Дубли в genre (не должно случаться в реальности) не должны задваивать
    # счётчик треков — жанр всё равно применяется к одним и тем же трекам.
    cache.mark_done("Artist A", ["rock", "rock"], _albums(4), None, "t")
    assert dump_tags.final_genre_track_counts(cache) == {"rock": 4}


def test_final_genre_artist_counts_counts_distinct_artists_per_genre(cache):
    cache.mark_done("Artist A", ["rock", "pop"], {}, None, "t")
    cache.mark_done("Artist B", ["rock"], {}, None, "t")
    cache.mark_done("Artist C", ["jazz"], {}, None, "t")

    counts = dump_tags.final_genre_artist_counts(cache)

    assert counts == {"rock": 2, "pop": 1, "jazz": 1}


def test_final_genre_artist_counts_ignores_artists_with_no_genre(cache):
    cache.mark_done("Artist A", None, {}, None, "t")
    assert dump_tags.final_genre_artist_counts(cache) == {}


def test_final_genre_artist_counts_counts_repeated_genre_once_per_artist(cache):
    # Не должно случаться в реальности (genre — список без дублей), но
    # set(genres) в реализации должен защитить от двойного счёта на всякий случай.
    cache.mark_done("Artist A", ["rock", "rock"], {}, None, "t")
    assert dump_tags.final_genre_artist_counts(cache) == {"rock": 1}


def test_main_ban_below_writes_genres_at_or_under_threshold_to_banlist(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "genres.db"
    banlist_path = tmp_path / "banlist.json"

    cache = Cache(str(db_path))
    cache.mark_done("Popular Artist", ["rock"], _albums(50), None, "t")  # 50 треков rock
    cache.mark_done("Obscure Artist", ["trumpet"], _albums(2), None, "t")  # 2 трека trumpet
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        [
            "dump_tags.py",
            "--db-path",
            str(db_path),
            "--ban-below",
            "10",
            "--banlist-path",
            str(banlist_path),
        ],
    )

    dump_tags.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"trumpet"}
    assert "trumpet" in capsys.readouterr().out


def test_main_ban_below_merges_with_existing_banlist(tmp_path, monkeypatch):
    from src.lastfm import save_banlist

    db_path = tmp_path / "genres.db"
    banlist_path = tmp_path / "banlist.json"
    save_banlist(str(banlist_path), frozenset({"already banned"}))

    cache = Cache(str(db_path))
    cache.mark_done("Obscure Artist", ["trumpet"], _albums(1), None, "t")
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        [
            "dump_tags.py",
            "--db-path",
            str(db_path),
            "--ban-below",
            "10",
            "--banlist-path",
            str(banlist_path),
        ],
    )

    dump_tags.main()

    from src.lastfm import load_banlist

    assert load_banlist(str(banlist_path)) == {"already banned", "trumpet"}


def test_main_suggest_bans_file_writes_editable_candidates(tmp_path, monkeypatch):
    db_path = tmp_path / "genres.db"
    suggest_path = tmp_path / "candidates.txt"

    cache = Cache(str(db_path))
    cache.mark_done("Popular Artist", ["rock"], _albums(50), None, "t")
    cache.mark_done("Obscure Artist", ["trumpet"], _albums(2), None, "t")
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        [
            "dump_tags.py",
            "--db-path",
            str(db_path),
            "--max-tracks",
            "10",
            "--suggest-bans-file",
            str(suggest_path),
        ],
    )

    dump_tags.main()

    content = suggest_path.read_text(encoding="utf-8")
    assert "trumpet" in content
    assert "rock" not in content  # 50 треков, не подходит под --max-tracks 10
    assert "2 track(s), 1 artist(s)" in content


def test_main_suggest_bans_file_excludes_already_banned_genres(tmp_path, monkeypatch):
    from src.lastfm import save_banlist

    db_path = tmp_path / "genres.db"
    banlist_path = tmp_path / "banlist.json"
    suggest_path = tmp_path / "candidates.txt"
    save_banlist(str(banlist_path), frozenset({"trumpet"}))

    cache = Cache(str(db_path))
    # trumpet ещё не пересчитан (--once не запускали), поэтому формально всё ещё
    # в track_counts — но раз он уже в бан-листе, повторно предлагать его не надо.
    cache.mark_done("Obscure Artist", ["trumpet"], _albums(2), None, "t")
    cache.mark_done("Another Artist", ["icelandic"], _albums(3), None, "t")
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        [
            "dump_tags.py",
            "--db-path",
            str(db_path),
            "--max-tracks",
            "10",
            "--banlist-path",
            str(banlist_path),
            "--suggest-bans-file",
            str(suggest_path),
        ],
    )

    dump_tags.main()

    content = suggest_path.read_text(encoding="utf-8")
    assert "icelandic" in content
    assert "trumpet" not in content


def test_main_no_clusters_suppresses_cluster_section(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "genres.db"

    cache = Cache(str(db_path))
    cache.mark_done(
        "Popular Artist", ["yandex music"], _albums(50), _raw([("yandex music", 100)]), "t"
    )
    cache.mark_done(
        "Obscure Artist", ["yander music"], _albums(2), _raw([("yander music", 100)]), "t"
    )
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        ["dump_tags.py", "--db-path", str(db_path), "--cutoff", "0.6", "--no-clusters"],
    )

    dump_tags.main()

    out = capsys.readouterr().out
    assert "объединение" not in out
    assert "не найдено" not in out


def test_main_max_tracks_keeps_cluster_with_at_least_one_rare_member(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "genres.db"

    cache = Cache(str(db_path))
    # 'yandex music' — популярный жанр с 50 треками. Он ДОЛЖЕН остаться в
    # выводе кластера при --max-tracks 3, потому что именно ради такой пары
    # (редкая опечатка + популярный оригинал) и нужны алиасы — иначе не с кем
    # будет сравнить 'yander music', чтобы понять, куда её мержить.
    cache.mark_done(
        "Popular Artist", ["yandex music"], _albums(50), _raw([("yandex music", 100)]), "t"
    )
    cache.mark_done(
        "Obscure Artist", ["yander music"], _albums(2), _raw([("yander music", 100)]), "t"
    )
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        ["dump_tags.py", "--db-path", str(db_path), "--cutoff", "0.6", "--max-tracks", "3"],
    )

    dump_tags.main()

    cluster_section = capsys.readouterr().out.rsplit("объединение", 1)[-1]
    assert "yandex music" in cluster_section
    assert "yander music" in cluster_section


def test_main_max_tracks_drops_cluster_where_all_members_are_above_threshold(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "genres.db"

    cache = Cache(str(db_path))
    # Оба варианта написания популярны (50 и 20 треков) — при --max-tracks 3
    # ни один не нуждается в разборе, кластер не должен показываться вовсе.
    cache.mark_done(
        "Popular Artist A", ["yandex music"], _albums(50), _raw([("yandex music", 100)]), "t"
    )
    cache.mark_done(
        "Popular Artist B", ["yander music"], _albums(20), _raw([("yander music", 100)]), "t"
    )
    cache.close()

    monkeypatch.setattr(
        "sys.argv",
        ["dump_tags.py", "--db-path", str(db_path), "--cutoff", "0.6", "--max-tracks", "3"],
    )

    dump_tags.main()

    out = capsys.readouterr().out
    assert "объединение" not in out  # заголовок секции с кластерами не печатается вовсе
    assert "не найдено (с учётом --max-tracks 3)" in out


def test_main_without_max_tracks_includes_popular_tags_in_cluster_pool(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "genres.db"

    cache = Cache(str(db_path))
    cache.mark_done(
        "Popular Artist", ["yandex music"], _albums(50), _raw([("yandex music", 100)]), "t"
    )
    cache.mark_done(
        "Obscure Artist", ["yander music"], _albums(2), _raw([("yander music", 100)]), "t"
    )
    cache.close()

    monkeypatch.setattr(
        "sys.argv", ["dump_tags.py", "--db-path", str(db_path), "--cutoff", "0.6"]
    )

    dump_tags.main()

    cluster_section = capsys.readouterr().out.rsplit("объединение", 1)[-1]
    assert "yandex music" in cluster_section
    assert "yander music" in cluster_section


def test_find_similar_clusters_groups_tags_missing_a_separator():
    tags = ["hip hop", "hiphop", "rock"]
    clusters = dump_tags.find_similar_clusters(tags, cutoff=0.8)
    assert ["hip hop", "hiphop"] in clusters
    assert not any("rock" in cluster for cluster in clusters)


def test_find_similar_clusters_does_not_group_unrelated_tags():
    tags = ["rock", "jazz", "hip hop"]
    clusters = dump_tags.find_similar_clusters(tags, cutoff=0.8)
    assert clusters == []


def test_find_similar_clusters_each_tag_appears_at_most_once():
    tags = ["synth pop", "synthpop", "synth-wave"]
    clusters = dump_tags.find_similar_clusters(tags, cutoff=0.6)
    seen = [tag for cluster in clusters for tag in cluster]
    assert len(seen) == len(set(seen))
