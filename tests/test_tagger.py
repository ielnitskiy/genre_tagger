import pytest
from mutagen import MutagenError
from mutagen.easyid3 import EasyID3

from src import tagger
from tests.conftest import make_corrupt_id3, make_mp3


def test_has_genre_false_when_no_id3_header(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    assert tagger.has_genre(str(path)) is False


def test_has_genre_false_when_id3_present_without_genre(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    tags = EasyID3()
    tags["title"] = ["Some Title"]
    tags.save(str(path), v2_version=4)
    assert tagger.has_genre(str(path)) is False


def test_has_genre_true_when_genre_set(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    tags = EasyID3()
    tags["genre"] = ["Rock"]
    tags.save(str(path), v2_version=4)
    assert tagger.has_genre(str(path)) is True


def test_has_genre_raises_on_corrupted_id3(tmp_path):
    path = tmp_path / "corrupt.mp3"
    make_corrupt_id3(path)
    with pytest.raises(MutagenError):
        tagger.has_genre(str(path))


def test_write_genre_sets_tag_on_fresh_file(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    tagger.write_genre(str(path), ["Rock", "Jazz"])
    assert EasyID3(str(path))["genre"] == ["Rock", "Jazz"]


def test_write_genre_does_not_overwrite_existing_without_force(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    tags = EasyID3()
    tags["genre"] = ["Existing"]
    tags.save(str(path), v2_version=4)

    tagger.write_genre(str(path), ["New Genre"], force=False)

    assert EasyID3(str(path))["genre"] == ["Existing"]


def test_write_genre_overwrites_existing_with_force(tmp_path):
    path = tmp_path / "a.mp3"
    make_mp3(path)
    tags = EasyID3()
    tags["genre"] = ["Existing"]
    tags.save(str(path), v2_version=4)

    tagger.write_genre(str(path), ["New Genre"], force=True)

    assert EasyID3(str(path))["genre"] == ["New Genre"]


def test_write_genre_raises_on_corrupted_id3_instead_of_swallowing(tmp_path):
    """Регрессия: раньше write_genre ловил ошибку has_genre и тихо возвращался,
    из-за чего файл пропускался навсегда без ретрая (см. scanner._tag_new_files)."""
    path = tmp_path / "corrupt.mp3"
    make_corrupt_id3(path)
    with pytest.raises(MutagenError):
        tagger.write_genre(str(path), ["Rock"])
