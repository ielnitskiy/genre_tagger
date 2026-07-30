import json

from src.genrelists import (
    fingerprint,
    load_aliases,
    load_banlist,
    migrate_json_if_needed,
)


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_banlist_missing_file_is_not_an_error(tmp_path):
    assert load_banlist(str(tmp_path / "nope.txt")) == frozenset()
    assert load_banlist(None) == frozenset()


def test_banlist_reads_lines_and_ignores_comments(tmp_path):
    path = _write(
        tmp_path / "banlist.txt",
        "# заголовок\n\nRussian\nyandex music  # служебный тег\n   \n",
    )
    assert load_banlist(path) == frozenset({"russian", "yandex music"})


def test_banlist_canonicalizes_spelling(tmp_path):
    path = _write(tmp_path / "banlist.txt", "Hip-Hop\nSKA_PUNK\n")
    assert load_banlist(path) == frozenset({"hip hop", "ska punk"})


def test_aliases_map_every_variant_to_its_main_genre(tmp_path):
    path = _write(tmp_path / "aliases.txt", "hip hop <- hiphop, hip-hop\nlo fi <- lofi\n")
    assert load_aliases(path) == {"hiphop": "hip hop", "lofi": "lo fi"}


def test_aliases_ignore_comments_and_blank_lines(tmp_path):
    path = _write(tmp_path / "aliases.txt", "# шапка\n\nlo fi <- lofi  # дубль написания\n")
    assert load_aliases(path) == {"lofi": "lo fi"}


def test_aliases_skip_malformed_line_but_keep_the_rest(tmp_path, caplog):
    """Файл правится руками, поэтому одна кривая строка не должна отменять
    остальные (в отличие от прежнего --add-alias-file со схемой всё-или-ничего)."""
    path = _write(tmp_path / "aliases.txt", "мусор без стрелки\nlo fi <- lofi\n")
    assert load_aliases(path) == {"lofi": "lo fi"}
    assert "expected" in caplog.text


def test_aliases_drop_variant_that_is_banned(tmp_path, caplog):
    path = _write(tmp_path / "aliases.txt", "rock <- russian rock\n")
    assert load_aliases(path, frozenset({"russian rock"})) == {}
    assert "banlist" in caplog.text


def test_aliases_drop_group_whose_main_genre_is_banned(tmp_path, caplog):
    path = _write(tmp_path / "aliases.txt", "russian <- russian rock\n")
    assert load_aliases(path, frozenset({"russian"})) == {}
    assert "banlist" in caplog.text


def test_aliases_resolve_chain_transitively(tmp_path, caplog):
    path = _write(tmp_path / "aliases.txt", "b <- a\nc <- b\n")
    assert load_aliases(path) == {"a": "c", "b": "c"}
    assert "chain" in caplog.text


def test_aliases_break_cycle_instead_of_hanging(tmp_path, caplog):
    path = _write(tmp_path / "aliases.txt", "b <- a\na <- b\n")
    assert load_aliases(path) == {}
    assert "cycle" in caplog.text


def test_aliases_keep_first_mapping_when_variant_listed_twice(tmp_path, caplog):
    path = _write(tmp_path / "aliases.txt", "rock <- indie\npop <- indie\n")
    assert load_aliases(path) == {"indie": "rock"}
    assert "already a variant" in caplog.text


def test_aliases_ignore_self_reference(tmp_path):
    path = _write(tmp_path / "aliases.txt", "rock <- rock, indie\n")
    assert load_aliases(path) == {"indie": "rock"}


def test_fingerprint_changes_with_content():
    a = fingerprint(frozenset({"pop"}), {})
    b = fingerprint(frozenset({"pop", "rock"}), {})
    c = fingerprint(frozenset({"pop"}), {"hiphop": "hip hop"})
    assert a != b != c and a != c


def test_fingerprint_is_stable_for_same_content():
    assert fingerprint(frozenset({"a", "b"}), {"x": "y"}) == fingerprint(
        frozenset({"b", "a"}), {"x": "y"}
    )


def test_migration_converts_legacy_json_files(tmp_path):
    (tmp_path / "genre_banlist.json").write_text(json.dumps(["Russian", "pop"]), encoding="utf-8")
    (tmp_path / "genre_aliases.json").write_text(
        json.dumps({"hip hop": ["hiphop", "trip hop"]}), encoding="utf-8"
    )
    banlist, aliases = str(tmp_path / "banlist.txt"), str(tmp_path / "aliases.txt")

    migrate_json_if_needed(banlist, aliases)

    assert load_banlist(banlist) == frozenset({"russian", "pop"})
    assert load_aliases(aliases) == {"hiphop": "hip hop", "trip hop": "hip hop"}


def test_migration_reads_the_oldest_flat_alias_format(tmp_path):
    (tmp_path / "genre_aliases.json").write_text(
        json.dumps({"hiphop": "hip hop"}), encoding="utf-8"
    )
    aliases = str(tmp_path / "aliases.txt")

    migrate_json_if_needed(str(tmp_path / "banlist.txt"), aliases)

    assert load_aliases(aliases) == {"hiphop": "hip hop"}


def test_migration_keeps_the_original_json_untouched(tmp_path):
    legacy = tmp_path / "genre_banlist.json"
    legacy.write_text(json.dumps(["pop"]), encoding="utf-8")

    migrate_json_if_needed(str(tmp_path / "banlist.txt"), str(tmp_path / "aliases.txt"))

    assert json.loads(legacy.read_text(encoding="utf-8")) == ["pop"]


def test_migration_does_not_overwrite_existing_txt(tmp_path):
    (tmp_path / "genre_banlist.json").write_text(json.dumps(["pop"]), encoding="utf-8")
    banlist = _write(tmp_path / "banlist.txt", "rock\n")

    migrate_json_if_needed(banlist, str(tmp_path / "aliases.txt"))

    assert load_banlist(banlist) == frozenset({"rock"})


def test_migration_is_a_noop_without_legacy_files(tmp_path):
    banlist, aliases = str(tmp_path / "banlist.txt"), str(tmp_path / "aliases.txt")
    migrate_json_if_needed(banlist, aliases)
    assert load_banlist(banlist) == frozenset()
    assert load_aliases(aliases) == {}
