def make_mp3(path, frames: int = 50) -> None:
    """Пишет синтетический, но валидный для mutagen MPEG-поток (без ID3-тега),
    достаточный чтобы EasyID3 могла открыть/создать/сохранить теги."""
    frame = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 100
    with open(path, "wb") as f:
        f.write(frame * frames)


def make_corrupt_id3(path) -> None:
    """Пишет файл с ID3-магией, но невалидным (не-synchsafe) размером заголовка —
    mutagen должен упасть с MutagenError при попытке прочитать теги."""
    with open(path, "wb") as f:
        f.write(b"ID3\x04\x00\x00\xff\xff\xff\xff" + b"\x00" * 10)
