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


def make_flac(path) -> None:
    """Минимальный FLAC без аудиоданных: маркер и блок STREAMINFO — этого
    достаточно, чтобы mutagen открыл файл и записал Vorbis comment."""
    streaminfo = (
        (4096).to_bytes(2, "big") + (4096).to_bytes(2, "big")  # min/max block size
        + (0).to_bytes(3, "big") + (0).to_bytes(3, "big")  # min/max frame size
        # 44100 Гц, 2 канала, 16 бит, 0 сэмплов, упакованные в 8 байт
        + bytes([0x0A, 0xC4, 0x42, 0xF0, 0x00, 0x00, 0x00, 0x00])
        + b"\x00" * 16  # MD5
    )
    header = bytes([0x80, 0x00, 0x00, len(streaminfo)])  # последний блок, тип 0
    with open(path, "wb") as f:
        f.write(b"fLaC" + header + streaminfo)
