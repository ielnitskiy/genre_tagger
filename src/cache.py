import json
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_genre (
    artist        TEXT PRIMARY KEY,
    genre         TEXT,
    albums        TEXT NOT NULL,
    raw_response  TEXT,
    processed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Отдельная таблица, а не колонка в artist_genre: --reset-artist удаляет
-- строку артиста из artist_genre целиком, но флаг force-rewrite должен
-- пережить это удаление и сработать на самом первом re-tag'е после ресета.
CREATE TABLE IF NOT EXISTS force_rewrite (
    artist TEXT PRIMARY KEY
);
"""


class Cache:
    def __init__(self, db_path: str):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # busy_timeout выше дефолтных 5с и WAL — чтобы конкурентный запуск
        # второго инстанса (например, ручной прогон поверх cron) ждал
        # освобождения блокировки вместо немедленного "database is locked",
        # и чтобы читатели не блокировались на время записи.
        self._conn = sqlite3.connect(db_path, timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_done(self, artist: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM artist_genre WHERE artist = ?", (artist,)
        )
        return cur.fetchone() is not None

    def get(self, artist: str) -> tuple[Optional[list[str]], dict]:
        cur = self._conn.execute(
            "SELECT genre, albums FROM artist_genre WHERE artist = ?", (artist,)
        )
        row = cur.fetchone()
        if row is None:
            return None, {}
        genre_json, albums_json = row
        genres = json.loads(genre_json) if genre_json is not None else None
        albums = json.loads(albums_json)
        return genres, albums

    def is_force_rewrite(self, artist: str) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM force_rewrite WHERE artist = ?", (artist,)
        )
        return cur.fetchone() is not None

    def set_force_rewrite(self, artist: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO force_rewrite (artist) VALUES (?)", (artist,)
        )
        self._conn.commit()

    def clear_force_rewrite(self, artist: str) -> None:
        self._conn.execute("DELETE FROM force_rewrite WHERE artist = ?", (artist,))
        self._conn.commit()

    def mark_done(
        self,
        artist: str,
        genres: Optional[list[str]],
        albums: dict,
        raw_json: Optional[str],
        timestamp: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO artist_genre
                (artist, genre, albums, raw_response, processed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                artist,
                json.dumps(genres) if genres is not None else None,
                json.dumps(albums),
                raw_json,
                timestamp,
            ),
        )
        self._conn.commit()

    def update_albums(self, artist: str, albums: dict) -> None:
        self._conn.execute(
            "UPDATE artist_genre SET albums = ? WHERE artist = ?",
            (json.dumps(albums), artist),
        )
        self._conn.commit()

    def update_genre(self, artist: str, genres: Optional[list[str]]) -> None:
        self._conn.execute(
            "UPDATE artist_genre SET genre = ? WHERE artist = ?",
            (json.dumps(genres) if genres is not None else None, artist),
        )
        self._conn.commit()

    def reset(self, artist: str) -> None:
        self._conn.execute("DELETE FROM artist_genre WHERE artist = ?", (artist,))
        self._conn.commit()

    def iter_with_raw_response(self) -> Iterator[tuple[str, str]]:
        cur = self._conn.execute(
            "SELECT artist, raw_response FROM artist_genre WHERE raw_response IS NOT NULL"
        )
        yield from cur.fetchall()

    def get_config_hash(self) -> Optional[str]:
        cur = self._conn.execute("SELECT value FROM meta WHERE key = 'config_hash'")
        row = cur.fetchone()
        return row[0] if row else None

    def set_config_hash(self, config_hash: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('config_hash', ?)",
            (config_hash,),
        )
        self._conn.commit()
