# genre_tagger

Проставляет жанр с Last.fm в ID3-тег (TCON) всех mp3 исполнителя в `/music`.
Периодически сканирует файловую систему, с ботом не связан. Архитектура и
алгоритм обхода — в `PLAN.md`, состояние чистки — в `CLEANUP.md`.

## Как выбирается жанр

```
теги Last.fm → выбросить служебные → aliases.txt → banlist.txt
            → только жанры из словаря MusicBrainz и genres_extra.txt → топ-3 по весу
```

Слева направо. Теги Last.fm ставят пользователи, поэтому сначала отсеивается
всё, что не жанр (`norway`, `russian`, `seen live`), и только потом берутся три
самых весомых. Меньше трёх — нормально. Словарь — `src/musicbrainz_genres.txt`
(снимок `musicbrainz.org/ws/2/genre/all`), написание жанра берётся из него
(`post-hardcore`). Сравнение не учитывает пробелы и дефисы: `synthpop` и
`synth-pop` — одно и то же.

Жанр в файлах переписывается всегда, даже если его поставил не `genre_tagger`
(старые рипы, spotDL). Если у исполнителя не нашлось ни одного жанра, тег
снимается.

## Три файла в `./data`

Правятся обычным редактором, `#` — комментарий, регистр и дефисы не важны.

```
genres_extra.txt     banlist.txt     aliases.txt
────────────────     ───────────     ─────────────────────────────────────
chillhop             audiobook       hip hop <- rap, hiphop
trancecore                           hardcore punk <- hardcore, nyhc
```

- `genres_extra.txt` — жанры, которых нет в словаре MusicBrainz, но они нужны.
- `banlist.txt` — настоящий жанр, который всё равно не нужен в библиотеке.
  После перехода на словарь мусор отсеивается сам, бан нужен редко.
- `aliases.txt` — сведение вариантов к одному жанру. Главный тег должен быть в
  словаре или в `genres_extra.txt`, иначе он отсеется.

Кривая строка пропускается с `WARNING`, остальные применяются. Бан сильнее
алиаса; цепочки `a → b → c` разворачиваются в `a → c`.

## Цепочки команд

Все команды выполняются из каталога проекта на сервере.

**Установка (один раз)**

```bash
cp .env.example .env && nano .env          # вписать LASTFM_API_KEY
docker compose build
docker compose run --rm genre-tagger --once --limit 50    # пробный прогон
docker compose run --rm genre-tagger --once               # вся библиотека
crontab -e                                                # строка ниже: 03:00 раз в двое суток
# 0 3 */2 * * cd /home/biobojlk/projects/media-server/genre_tagger && /usr/bin/docker compose run --rm genre-tagger --once >> /home/biobojlk/projects/media-server/genre_tagger/data/cron.log 2>&1
```

**Регулярное обслуживание** — раз в несколько месяцев, когда библиотека подросла

```bash
docker compose run --rm genre-tagger --report        # посмотреть, что накопилось
nano data/banlist.txt                                # дописать мусор из низа списка
nano data/aliases.txt                                # свести дубли из второй секции отчёта
docker compose run --rm genre-tagger --once          # пересчёт всей библиотеки, без сети
docker compose run --rm genre-tagger --report        # проверить результат
```

**Обновление кода**

```bash
git pull
docker compose build --no-cache
docker compose run --rm genre-tagger --once
```

**Один исполнитель** — имя должно точно совпадать с именем папки

```bash
docker compose run --rm genre-tagger --tag-artist "Имя"     # заново спросить Last.fm, сразу
docker compose run --rm genre-tagger --reset-artist "Имя"   # то же, но на следующем проходе
```

**Диагностика**

```bash
tail -f data/cron.log                                      # логи cron
docker compose run --rm genre-tagger --once --force-scan   # игнорировать mtime-gate
docker compose logs genre-tagger
```

**Аварийный сброс**

```bash
docker compose run --rm genre-tagger --wipe-all-genres        # dry-run: сколько файлов затронет
docker compose run --rm genre-tagger --wipe-all-genres --yes  # снять все жанры + сбросить кэш
rm data/genres.db                                             # полный сброс, Last.fm опросится заново
```

## Что важно помнить

- **`--once` после правки файлов ничего не качает.** Жанры пересчитываются из
  сохранённых ответов Last.fm; ID3 переписывается только там, где результат
  изменился.
- **`--report` ничего не меняет** и в сеть не ходит.
- **Пути внутри контейнера — `/data/...`**, а не `./data/...`: `WORKDIR=/app`,
  том смонтирован как `./data:/data`.
- **Старые `genre_banlist.json`/`genre_aliases.json`** конвертируются в `.txt`
  при первом `--once`; исходники остаются на диске.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `LASTFM_API_KEY` | — (обязательна) | Ключ Last.fm, read-only метод |
| `MUSIC_DIR` | `/music` | Корень медиатеки (`{artist}/{album}/{title}.mp3`) |
| `DB_PATH` | `/data/genres.db` | Кэш "исполнитель уже обработан" |
| `BANLIST_FILE` | `/data/banlist.txt` | Запрещённые жанры |
| `ALIASES_FILE` | `/data/aliases.txt` | Синонимы |
| `GENRES_EXTRA_FILE` | `/data/genres_extra.txt` | Жанры сверх словаря MusicBrainz |
| `GENRE_TTL_DAYS` | `180` | Когда перепроверить жанр у Last.fm |
| `SCAN_INTERVAL_SECONDS` | `86400` | Пауза между проходами без `--once` (при cron не используется) |
| `SKIP_DIRS` | `download-errors` | Папки, которые не считаются исполнителями |
| `LOG_LEVEL` | `INFO` | Уровень логирования |

## Тесты

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

## Известные ограничения

1. **Нет тегов на Last.fm** → `genre=NULL` до `GENRE_TTL_DAYS`; форсировать —
   `--reset-artist`.
2. **Имя папки ≠ каноническое имя на Last.fm** → `genre=None`; `autocorrect=1`
   помогает лишь частично.
3. **Однофамильцы получают чужие жанры.** Жанр резолвится по имени папки, а
   Last.fm отдаёт теги всех групп с таким именем вперемешку. Реальный пример:
   папка `Scotch` (русская группа) получила `emo, metalcore, italo disco` —
   последний тег от итальянской italo-disco группы 80-х с тем же названием.
   `--reset-artist`/`--tag-artist` не помогут: ответ Last.fm будет тем же.
   Лечится только правкой тега вручную; если случаев накопится много, имеет
   смысл завести файл ручных переопределений `артист -> жанры`.
4. **Один жанр на всю папку** — сборники и "Various Artists" получат общий.
5. **Жанр, которого нет в словаре MusicBrainz, отсеивается**, даже если он
   осмысленный (`russian rock`, `chillhop`). Нужный — в `genres_extra.txt`.
6. **mtime-gate** полагается на обновление mtime папки при изменении содержимого
   — на сетевых ФС (NFS/SMB) стоит проверить, иначе `--force-scan`.
7. **Осиротевшие записи кэша** (папка удалена с диска) не чистятся сами, только
   `WARNING` на каждом проходе.
