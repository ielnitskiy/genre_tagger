import json

import pytest

from scripts import dump_tags
from src.cache import Cache


def _raw(tags):
    return json.dumps({"toptags": {"tag": [{"name": n, "count": str(c)} for n, c in tags]}})


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
