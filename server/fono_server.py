# -*- coding: utf-8 -*-
"""
Локальный сервер Фонотеки:
  - отдаёт fonoteka.html и сохранённые треки;
  - /api/search?q=...  — поиск песен (yt-dlp);
  - /api/download      — скачивание полной песни в папку Fonoteka.
Запускается через "Запустить Фонотеку.bat".
"""
import os, sys, json, glob, re, shutil, socket, concurrent.futures
import urllib.request, urllib.parse
from html import unescape as _unescape
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from yt_dlp import YoutubeDL

# Локально — 8753. В облаке (Render/Fly/Koyeb и т.п.) хостинг сам назначает порт
# и передаёт его через переменную окружения PORT — читаем её, если задана.
PORT = int(os.environ.get("PORT") or 8753)
# Есть ли ffmpeg? На облачном сервере (Docker) — да, тогда из Rutube извлекаем
# ЧИСТОЕ АУДИО (браузер не проигрывает видео-mp4 в аудио-плеере). Локально без
# ffmpeg — остаётся видео-mp4 (как было).
_HAS_FFMPEG = bool(shutil.which("ffmpeg"))
# Таймаут на сетевые операции yt-dlp — чтобы одна зависшая страница не тянула
# поиск/стрим/скачивание к «минуте». Зависший источник просто отвалится быстро.
NET_TIMEOUT = 10          # сек на один сетевой запрос yt-dlp
SEARCH_DEADLINE = 12      # сек — общий потолок ожидания всех источников поиска

# Корень: рядом с .exe (собранная программа) либо рядом со скриптом (обычный запуск).
if getattr(sys, "frozen", False):
    ROOT = os.path.dirname(sys.executable)
    # Кладём интерфейс рядом с .exe и ПЕРЕЗАПИСЫВАЕМ его, если он отличается от
    # версии внутри exe. Раньше писали "только если файла ещё нет" — из-за этого
    # свежий exe, попавший в папку со старым fonoteka.html (напр. Downloads),
    # показывал древний интерфейс. Теперь каждая версия exe несёт свой UI.
    _bundled = os.path.join(getattr(sys, "_MEIPASS", ROOT), "fonoteka.html")
    _dst = os.path.join(ROOT, "fonoteka.html")
    if os.path.exists(_bundled):
        try:
            _need = True
            if os.path.exists(_dst):
                with open(_bundled, "rb") as _a, open(_dst, "rb") as _b:
                    _need = _a.read() != _b.read()
            if _need:
                shutil.copyfile(_bundled, _dst)
        except Exception:
            pass
else:
    ROOT = os.path.dirname(os.path.abspath(__file__))

MUSIC_DIR = os.path.join(ROOT, "Fonoteka")
os.makedirs(MUSIC_DIR, exist_ok=True)

def _safe_key(source, vid):
    s = str(vid)
    # если вместо id передали ссылку — вытащим из неё чистый video-id YouTube,
    # иначе имя файла превращается в "youtube-https___www_youtube_com_watch_v_…"
    # и мусорит в библиотеке (папка Fonoteka подтягивается как список песен).
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{6,})", s)
    if m:
        s = m.group(1)
    return source + "-" + re.sub(r"[^A-Za-z0-9_-]", "_", s)[:60]

def _provider_search(query, prefix, n, source, use_cookies=False):
    """Один источник (ytsearch / scsearch) через API yt-dlp (внутри процесса)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": True, "ignoreerrors": True,
            "socket_timeout": NET_TIMEOUT}
    if use_cookies:
        opts["cookiesfrombrowser"] = ("firefox",)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"{prefix}{n}:{query}", download=False)
    except Exception:
        return []
    items = []
    for d in ((info or {}).get("entries") or []):
        if not d:
            continue
        vid = d.get("id")
        if not vid:
            continue
        url = d.get("url") or d.get("webpage_url") or ""
        if not str(url).startswith("http"):
            url = f"https://www.youtube.com/watch?v={vid}" if source == "youtube" else ""
        if not url:
            continue
        if source == "youtube":
            thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
        else:
            ths = d.get("thumbnails") or []
            thumb = (ths[-1].get("url", "") if ths else "")
        items.append({
            "key": _safe_key(source, vid),
            "url": url,
            "title": d.get("title") or "",
            "uploader": d.get("uploader") or d.get("channel") or d.get("uploader_id") or "",
            "duration": d.get("duration"),
            "thumb": thumb,
            "source": source,
        })
    return items

def _http_json(url, data=None, headers=None, timeout=12):
    """Простой GET/POST → JSON (для каталогов без ключа: Deezer/iTunes/Bandcamp)."""
    h = {"User-Agent": "Mozilla/5.0 (Fonoteka)"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))

def _deezer_search(query, n=60):
    """Каталог Deezer (без ключа). Метаданные + 30-сек превью; полный трек
    докачиваем с YouTube по «исполнитель название» (как spotdl)."""
    try:
        url = "https://api.deezer.com/search?limit=%d&q=%s" % (n, urllib.parse.quote(query))
        data = _http_json(url)
    except Exception:
        return []
    items = []
    for d in (data.get("data") or []):
        title = (d.get("title") or "").strip()
        artist = ((d.get("artist") or {}).get("name") or "").strip()
        if not title:
            continue
        items.append({
            "key": _safe_key("deezer", d.get("id")),
            "url": "ytsearch1:%s %s" % (artist, title),
            "title": title,
            "uploader": artist,
            "duration": d.get("duration"),
            "thumb": (d.get("album") or {}).get("cover_medium") or "",
            "source": "deezer",
            "preview": d.get("preview") or "",
        })
    return items

def _itunes_search(query, n=60):
    """Каталог Apple Music (iTunes Search API, без ключа). Полный трек — с YouTube."""
    try:
        url = ("https://itunes.apple.com/search?media=music&entity=song&limit=%d&term=%s"
               % (n, urllib.parse.quote(query)))
        data = _http_json(url)
    except Exception:
        return []
    items = []
    for d in (data.get("results") or []):
        title = (d.get("trackName") or "").strip()
        artist = (d.get("artistName") or "").strip()
        if not title:
            continue
        thumb = (d.get("artworkUrl100") or "").replace("100x100bb", "300x300bb").replace("100x100", "300x300")
        ms = d.get("trackTimeMillis") or 0
        items.append({
            "key": _safe_key("apple", d.get("trackId")),
            "url": "ytsearch1:%s %s" % (artist, title),
            "title": title,
            "uploader": artist,
            "duration": int(ms / 1000) or None,
            "thumb": thumb,
            "source": "apple",
            "preview": d.get("previewUrl") or "",
        })
    return items

def _bandcamp_search(query, n=40):
    """Каталог Bandcamp (публичный autocomplete API). У треков есть прямая
    ссылка — её yt-dlp качает/стримит напрямую (без YouTube-резолва)."""
    try:
        body = json.dumps({"search_text": query, "search_filter": "t",
                           "full_page": False, "fan_id": None}).encode("utf-8")
        data = _http_json("https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic",
                          data=body, headers={"Content-Type": "application/json"})
    except Exception:
        return []
    items = []
    for d in ((data.get("auto") or {}).get("results") or []):
        if d.get("type") != "t":      # только треки (не альбомы/исполнители)
            continue
        url = d.get("item_url_path") or d.get("url") or ""
        title = (d.get("name") or "").strip()
        if not title or not str(url).startswith("http"):
            continue
        items.append({
            "key": _safe_key("bandcamp", d.get("id") or url),
            "url": url,
            "title": title,
            "uploader": (d.get("band_name") or "").strip(),
            "duration": None,
            "thumb": d.get("img") or "",
            "source": "bandcamp",
        })
        if len(items) >= n:
            break
    return items

# Фильтр «только музыка» для Rutube (это общий видеосервис). У каждого видео есть
# КАТЕГОРИЯ — по ней надёжнее всего:
_RT_MUSIC_CATS = {6, 48}                 # «Музыка», «Аудио» — точно музыка, берём всегда
# БЕЛЫЙ СПИСОК «где тоже бывают песни/клипы» (но много и не-музыки) — берём только
# короткое и без «обзорных» слов. Всё, чего нет ни тут, ни в MUSIC_CATS
# (авто, техника, обзоры, новости, спорт, фильмы, наука и т.д.) — ВЫКИДЫВАЕМ.
_RT_MAYBE_CATS = {13, 15, 19, 22, 57, 73}   # Разное, Люди/блоги, Юмор, Игры, Развлечения, Лайфстайл
# Слова в названии — доп. отсев для «неоднозначных» категорий.
_RT_NOT_MUSIC = ("обзор", "распаковк", "unboxing", "review", "сравнени", "подкаст",
                 "стрим", "прохожден", "летсплей", "gameplay", "влог", "vlog",
                 "новости", "трейлер", "интервью", "туториал", "tutorial",
                 "лайфхак", "реклама", "как сделать", "своими руками", "выпуск ",
                 "смартфон", "гаджет", "характеристики", "прошивк", "андроид",
                 "первый взгляд", "тест-драйв", "тестируем", "инструкция")

def _rutube_search(query, n=30):
    """Каталог Rutube — российский аналог YouTube. Публичный поиск без ключа и
    без входа в аккаунт (параметр client=wdp обязателен, иначе выдача пустая).
    Главное преимущество для ОНЛАЙН-размещения Фонотеки: серверы Rutube не режут
    запросы из дата-центров, как это делает YouTube, поэтому и поиск, и
    скачивание работают с обычного хостинга. Треки yt-dlp качает напрямую."""
    try:
        url = ("https://rutube.ru/api/search/video/?client=wdp&query=%s"
               % urllib.parse.quote(query))
        data = _http_json(url)
    except Exception:
        return []
    items = []
    for d in (data.get("results") or []):
        title = (d.get("title") or "").strip()
        vurl = d.get("video_url") or ""
        if not title or not str(vurl).startswith("http"):
            continue
        # ФИЛЬТР «ТОЛЬКО МУЗЫКА» (Rutube — общий видеосервис):
        cat = (d.get("category") or {}).get("id")
        dur = d.get("duration") or 0
        low = title.lower()
        if cat in _RT_MUSIC_CATS:
            if dur and dur > 1800:                # музыка, но часовой микс — мимо
                continue
        elif cat in _RT_MAYBE_CATS:
            # «может быть музыка» — берём только короткое (20с..8мин) и без слов-обзоров
            if dur and (dur < 20 or dur > 480):
                continue
            if any(w in low for w in _RT_NOT_MUSIC):
                continue
        else:
            continue                              # не музыкальная категория — выкидываем
        items.append({
            "key": _safe_key("rutube", d.get("id") or vurl),
            "url": vurl,
            "title": title,
            "uploader": ((d.get("author") or {}).get("name") or "").strip(),
            "duration": d.get("duration"),
            "thumb": d.get("thumbnail_url") or "",
            "source": "rutube",
        })
        if len(items) >= n:
            break
    return items

# ============================================================================
#  ПОИСК КНИГ / АУДИОКНИГ  (раздел «Книги» в интерфейсе)
#  akniga.org и audioknigi.pro не отдают JSON и режут CORS, поэтому их страницы
#  поиска парсим здесь, на локальном сервере, и отдаём фронту готовый JSON.
#  Прямой аудиопоток эти сайты прячут в обфусцированном плеере — надёжно его не
#  вытащить, поэтому у книги есть ссылка на страницу (кнопка «Открыть на сайте»).
# ============================================================================

def _http_text(url, data=None, headers=None, timeout=12):
    """GET/POST → распакованный HTML-текст (для сайтов без API)."""
    h = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"),
         "Accept-Language": "ru,en;q=0.9"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

def _strip_tags(s):
    return _unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

def _akniga_search(query, n=24):
    """akniga.org — GET-страница поиска. Заголовок берём из alt обложки
    (формат «Автор – Название»)."""
    try:
        html = _http_text("https://akniga.org/search/books/?q=" +
                          urllib.parse.quote(query))
    except Exception:
        return []
    items = []
    for chunk in html.split("content__main__articles--item")[1:]:
        m_url = re.search(r'href="(https://akniga\.org/[^"#?]+)"', chunk)
        if not m_url:
            continue
        m_img = re.search(r'<img[^>]*?src="([^"]+)"', chunk)
        m_alt = re.search(r'<img[^>]*?alt="([^"]*)"', chunk)
        alt = _unescape(m_alt.group(1)) if m_alt else ""
        author, title = "", alt
        for dash in (" – ", " — ", " - "):
            if dash in alt:
                author, title = alt.split(dash, 1)
                break
        title = title.strip()
        if not title:
            continue
        items.append({
            "title": title,
            "author": author.strip(),
            "cover": m_img.group(1) if m_img else "",
            "url": m_url.group(1),
            "source": "akniga",
            "type": "audio",
        })
        if len(items) >= n:
            break
    return items

def _audioknigi_search(query, n=24):
    """audioknigi.pro — движок DLE: поиск через POST index.php?do=search."""
    try:
        body = urllib.parse.urlencode({
            "do": "search", "subaction": "search",
            "search_start": "0", "full_search": "0", "result_from": "1",
            "story": query,
        }).encode("utf-8")
        html = _http_text("https://audioknigi.pro/index.php?do=search", data=body,
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    except Exception:
        return []
    items = []
    for chunk in html.split('class="short short-nm')[1:]:
        m_url = re.search(r'href="(https?://audioknigi\.pro/audioknigi/[^"]+\.html)"', chunk)
        if not m_url:
            continue
        m_t = re.search(r'class="name-kniga"[^>]*>(.*?)</a>', chunk, re.S)
        title = _strip_tags(m_t.group(1)) if m_t else ""
        if not title:
            continue
        m_img = re.search(r'class="short-img".*?<img[^>]*?(?:data-src|src)="([^"]+)"', chunk, re.S)
        cover = m_img.group(1) if m_img else ""
        if cover.startswith("/"):
            cover = "https://audioknigi.pro" + cover
        m_a = re.search(r'class="author">(.*?)</span>', chunk, re.S)
        items.append({
            "title": title,
            "author": _strip_tags(m_a.group(1)) if m_a else "",
            "cover": cover,
            "url": m_url.group(1),
            "source": "audioknigi",
            "type": "audio",
        })
        if len(items) >= n:
            break
    return items

def _knigavuhe_search(query, n=24):
    """knigavuhe.org — GET-страница поиска. Заголовок из alt обложки,
    автор из ссылки /author/."""
    try:
        html = _http_text("https://knigavuhe.org/search/?q=" +
                          urllib.parse.quote(query))
    except Exception:
        return []
    items = []
    for chunk in html.split('class="bookkitem"')[1:]:
        m_url = re.search(r'href="(/book/[^"#?]+)"', chunk)
        if not m_url:
            continue
        m_img = re.search(r'src="([^"]+covers[^"]+)"', chunk)
        m_t = (re.search(r'bookkitem_cover_img[^>]*alt="([^"]*)"', chunk)
               or re.search(r'alt="([^"]*)"', chunk))
        title = _unescape(m_t.group(1)) if m_t else ""
        if not title:
            continue
        m_a = re.search(r'bookkitem_author.*?<a href="/author/[^"]*">([^<]+)', chunk, re.S)
        items.append({
            "title": title,
            "author": _strip_tags(m_a.group(1)) if m_a else "",
            "cover": m_img.group(1) if m_img else "",
            "url": "https://knigavuhe.org" + m_url.group(1),
            "source": "knigavuhe",
            "type": "audio",
        })
        if len(items) >= n:
            break
    return items

def _youtube_book_search(query, n=15):
    """YouTube через yt-dlp: ищем «аудиокнига <запрос>». Аудио играется прямо
    в приложении — фронт берёт прямой поток через /api/stream (как музыка)."""
    res = _provider_search("аудиокнига " + query, "ytsearch", n, "youtube")
    if not res:
        res = _provider_search("аудиокнига " + query, "ytsearch", n, "youtube", use_cookies=True)
    out = []
    for r in res:
        out.append({
            "title": r.get("title") or "",
            "author": r.get("uploader") or "",
            "cover": r.get("thumb") or "",
            "url": r.get("url") or "",
            "source": "youtube",
            "type": "audio",
            "yt": r.get("url") or "",          # прямой поток дотянем при клике «Слушать»
            "duration": r.get("duration"),
        })
    return out

def _rutube_book_search(query, n=20):
    """Аудиокниги с Rutube — играются ПРЯМО в приложении через встроенный плеер
    (rutube.ru/play/embed/<id>), без «перехода на сайт». Rutube не блокирует
    серверы, поэтому работает и в облаке. Ищем «<запрос> аудиокнига»."""
    try:
        url = ("https://rutube.ru/api/search/video/?client=wdp&query=%s"
               % urllib.parse.quote(query + " аудиокнига"))
        data = _http_json(url)
    except Exception:
        return []
    out = []
    for d in (data.get("results") or []):
        vid = d.get("id")
        title = (d.get("title") or "").strip()
        if not vid or not title:
            continue
        out.append({
            "title": title,
            "author": ((d.get("author") or {}).get("name") or "").strip(),
            "cover": d.get("thumbnail_url") or "",
            "url": d.get("video_url") or "",
            "source": "rutube",
            "type": "audio",
            "rt": vid,                       # id для встроенного плеера (embed)
            "duration": d.get("duration"),
        })
        if len(out) >= n:
            break
    return out

# akniga/audioknigi/КнигаВухе убраны: их звук нельзя воспроизвести в приложении
# (сайты его прячут), оставались только «Открыть на сайте». Взамен — Rutube
# (встроенный плеер, играет внутри) + YouTube. Плюс клиентские LibriVox/Архив.
BOOK_SOURCES = {
    "rutube": lambda q: _rutube_book_search(q, 20),
    "youtube": lambda q: _youtube_book_search(q, 15),
}
BOOK_ORDER = ["rutube", "youtube"]

# В облаке (FONO_NO_YT) YouTube-книги тоже убираем: их поиск через yt-dlp на
# сервере ВИСНЕТ (YouTube блокирует дата-центр) → книжный поиск тормозит на
# 30 сек, а клик по такой книге открывал бы сам YouTube. Остаётся Rutube
# (+ клиентские LibriVox/Архив, они грузятся прямо в браузере).
if os.environ.get("FONO_NO_YT"):
    BOOK_SOURCES.pop("youtube", None)
    BOOK_ORDER = [s for s in BOOK_ORDER if s in BOOK_SOURCES]

def book_search(query, sources=None):
    """Поиск аудиокниг по akniga.org / audioknigi.pro параллельно, чередуя."""
    chosen = [s for s in (sources or BOOK_ORDER) if s in BOOK_SOURCES] or list(BOOK_ORDER)
    by_src = {s: [] for s in chosen}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen)) as ex:
        futs = {ex.submit(BOOK_SOURCES[s], query): s for s in chosen}
        try:
            for f in concurrent.futures.as_completed(futs, timeout=SEARCH_DEADLINE):
                try:
                    by_src[futs[f]] = f.result() or []
                except Exception:
                    by_src[futs[f]] = []
        except concurrent.futures.TimeoutError:
            pass
    merged = _interleave([by_src.get(s, []) for s in BOOK_ORDER if s in chosen])
    return {"results": merged}

# каждый источник: query -> список треков (в едином формате)
# YouTube/SoundCloud идут через yt-dlp с пагинацией (каждые ~20 результатов =
# отдельный запрос), поэтому большие n делают поиск медленным. Урезаны ради
# скорости. Deezer/Apple/Bandcamp — это один быстрый JSON-запрос, их оставляем
# большими (результатов всё равно много, но поиск почти не тормозит).
SOURCES = {
    "youtube":    lambda q: _provider_search(q, "ytsearch", 40, "youtube"),
    "rutube":     lambda q: _rutube_search(q, 30),
    "soundcloud": lambda q: _provider_search(q, "scsearch", 25, "soundcloud"),
    "deezer":     lambda q: _deezer_search(q, 60),
    "apple":      lambda q: _itunes_search(q, 60),
    "bandcamp":   lambda q: _bandcamp_search(q, 40),
}
SOURCE_ORDER = ["youtube", "rutube", "soundcloud", "deezer", "apple", "bandcamp"]

# В ОБЛАКЕ (переменная FONO_NO_YT=1, задаётся в Dockerfile): убираем YouTube и
# источники, которые качают ПОЛНЫЙ трек через YouTube (Deezer/Apple резолвят его
# с ютуба → на сервере в дата-центре не сработают, там YouTube блокирует ботов).
# Остаются те, что качаются НАПРЯМУЮ и серверы не блокируют: Rutube, SoundCloud,
# Bandcamp. На домашнем ПК (без этой переменной) YouTube остаётся как раньше.
if os.environ.get("FONO_NO_YT"):
    for _s in ("youtube", "deezer", "apple"):
        SOURCES.pop(_s, None)
    SOURCE_ORDER = [s for s in SOURCE_ORDER if s in SOURCES]

def _interleave(lists):
    """Чередуем источники по кругу, чтобы результаты каждого сайта были видны
    сверху, а не тонули под 120 ютубовскими."""
    out, i = [], 0
    while True:
        added = False
        for lst in lists:
            if i < len(lst):
                out.append(lst[i]); added = True
        if not added:
            break
        i += 1
    return out

def search(query, sources=None):
    """Поиск по нескольким каталогам параллельно, результаты чередуются."""
    chosen = [s for s in (sources or SOURCE_ORDER) if s in SOURCES]
    if not chosen:
        chosen = list(SOURCE_ORDER)
    by_src = {s: [] for s in chosen}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen)) as ex:
        futs = {ex.submit(SOURCES[s], query): s for s in chosen}
        # Ждём не дольше SEARCH_DEADLINE: медленный источник (обычно YouTube)
        # просто не попадёт в выдачу, но поиск вернётся быстро, а не «за минуту».
        try:
            for f in concurrent.futures.as_completed(futs, timeout=SEARCH_DEADLINE):
                s = futs[f]
                try:
                    by_src[s] = f.result() or []
                except Exception:
                    by_src[s] = []
        except concurrent.futures.TimeoutError:
            pass
    # YouTube иногда блокирует поиск как «бота» → пусто. Повторяем с cookies
    # из Firefox, чтобы поиск не отдавал «ничего не найдено» на ровном месте.
    if "youtube" in chosen and not by_src.get("youtube"):
        by_src["youtube"] = _provider_search(query, "ytsearch", 40, "youtube", use_cookies=True)
    merged = _interleave([by_src.get(s, []) for s in SOURCE_ORDER if s in chosen])
    out, seen = [], set()
    for it in merged:
        k = (it["title"].lower().strip(), (it["uploader"] or "").lower().strip())
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return {"results": out}

def _resolve_stream(url, use_cookies, fast=True):
    """Прямая ссылка на аудиопоток БЕЗ скачивания файла — для мгновенного
    прослушивания (браузер стримит прогрессивно, играть начинает за 1-2 сек)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "noplaylist": True, "ignoreerrors": True,
            "socket_timeout": NET_TIMEOUT}
    if fast:
        # один быстрый плеер-клиент вместо нескольких — извлечение в разы короче.
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
    if use_cookies:
        opts["cookiesfrombrowser"] = ("firefox",)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return None, str(e)
    if not info:
        return None, "no info"
    # ytsearch1:/scsearch: и т.п. отдают плейлист — берём первый трек
    if info.get("entries") is not None:
        ents = [e for e in (info.get("entries") or []) if e]
        if not ents:
            return None, "нет результатов"
        info = ents[0]
    best, best_abr = None, -1.0
    for f in (info.get("formats") or []):
        if f.get("acodec") in (None, "none"):
            continue
        if f.get("vcodec") not in (None, "none"):
            continue  # только аудио-дорожки
        if (f.get("protocol") or "") not in ("https", "http"):
            continue  # без HLS/DASH — браузер их сам не играет
        u = f.get("url") or ""
        if not str(u).startswith("http"):
            continue
        abr = float(f.get("abr") or f.get("tbr") or 0)
        if abr > best_abr:
            best, best_abr = f, abr
    if best:
        return best["url"], ""
    u = info.get("url")
    if u and str(u).startswith("http"):
        return u, ""
    return None, "нет прогрессивного аудиопотока"

def _rutube_hls(vid):
    """HLS-поток (m3u8) Rutube по id — через публичный play/options. Нужен, чтобы
    СТРИМИТЬ аудиокнигу (10 часов не скачаешь), а браузер играл её на лету."""
    try:
        u = "https://rutube.ru/api/play/options/%s/?format=json&no_404=true" % vid
        d = _http_json(u)
        vb = d.get("video_balancer") or {}
        return vb.get("m3u8") or vb.get("default") or ""
    except Exception:
        return ""

def _found(key):
    return [f for f in glob.glob(os.path.join(MUSIC_DIR, key + ".*")) if not f.endswith(".part")]

def _run_dl(url, key, use_cookies, fast=True):
    outtmpl = os.path.join(MUSIC_DIR, key + ".%(ext)s")
    # Rutube отдаёт только «видео целиком» (отдельной аудиодорожки нет), поэтому
    # берём САМОЕ ЛЁГКОЕ качество (144p ≈ 165 kbps) — для музыки на слух этого
    # достаточно, а файл выходит небольшим (~6 МБ на песню). Браузер играет звук
    # прямо из такого mp4. Ютубовские player_client/cookies для Rutube не нужны.
    is_rutube = key.startswith("rutube-") or "rutube.ru" in url
    opts = {
        "format": ("worst[height<=144]/worst[height<=360]/worst" if is_rutube
                   else "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"),
        "noplaylist": True,
        "outtmpl": outtmpl,
        "quiet": True, "no_warnings": True,
        "socket_timeout": NET_TIMEOUT,
        # качаем фрагменты параллельно (для дроблёных DASH-потоков) — для обычного
        # цельного аудио безвредно, для фрагментированного ускоряет в разы.
        "concurrent_fragment_downloads": 5,
    }
    if is_rutube and _HAS_FFMPEG:
        # на сервере есть ffmpeg → вытаскиваем ЧИСТОЕ АУДИО (m4a) без видео:
        # тогда трек нормально играет в <audio> (видео-mp4 браузер часто не тянет)
        # и это же делает книги «аудио-только», без картинки.
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    if fast and not is_rutube:
        # ОСНОВНОЕ время уходит не на закачку байтов (она <0.5с), а на извлечение:
        # yt-dlp по умолчанию опрашивает несколько плеер-клиентов YouTube. Берём
        # один быстрый (android_vr) → извлечение в разы короче. Запасной проход
        # (fast=False) опрашивает все клиенты — на случай, если этот сломается.
        opts["extractor_args"] = {"youtube": {"player_client": ["android_vr"]}}
    if use_cookies and not is_rutube:
        opts["cookiesfrombrowser"] = ("firefox",)
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        return ""
    except Exception as e:
        return str(e)

def download(url, key):
    existing = _found(key)
    if not existing:
        last_err = ""
        # сначала быстрый путь без cookies; если YouTube попросит проверку —
        # повтор с cookies из Firefox (обход бот-защиты). Так обычная песня
        # качается заметно быстрее (не читаем базу cookies каждый раз).
        # yt-dlp сам скачивает первый результат прямо по «ytsearch1:…», поэтому
        # отдельный резолв-проход (целая лишняя экстракция) больше не нужен.
        # проход 1: быстрый клиент без cookies (≈1.5с в обычном случае);
        # проход 2 (запасной): все клиенты + cookies — обход бот-защиты/поломки.
        for fast, use_cookies in ((True, False), (False, True)):
            last_err = (_run_dl(url, key, use_cookies, fast) or "")[-400:]
            existing = _found(key)
            if existing:
                break
        if not existing:
            return {"error": last_err or "не удалось скачать"}
    fname = os.path.basename(existing[0])
    return {"ok": True, "file": fname, "url": "/Fonoteka/" + fname}

_AUDIO_EXT = (".m4a", ".mp3", ".webm", ".opus", ".ogg", ".wav", ".flac", ".aac", ".mp4")

def library():
    """Список всех скачанных файлов на диске — чтобы восстановить библиотеку,
    если база браузера (IndexedDB) очистилась, а сами песни целы."""
    # В облаке диск ОБЩИЙ и ВРЕМЕННЫЙ (стирается при перезапуске) — восстановление
    # оттуда засоряет «Мою музыку» чужими/битыми следами. Отключаем: у каждого своя
    # библиотека в браузере, а треки играются потоком.
    if os.environ.get("FONO_NO_YT"):
        return {"results": []}
    out = []
    try:
        for f in sorted(os.listdir(MUSIC_DIR)):
            if f.endswith(".part") or not f.lower().endswith(_AUDIO_EXT):
                continue
            full = os.path.join(MUSIC_DIR, f)
            if not os.path.isfile(full):
                continue
            key = os.path.splitext(f)[0]
            low = key.lower()
            if low.startswith("youtube-"):
                source, vid = "youtube", key[len("youtube-"):]
            elif low.startswith("rutube-"):
                source, vid = "rutube", ""
            elif low.startswith("soundcloud-"):
                source, vid = "soundcloud", ""
            elif low.startswith("deezer-"):
                source, vid = "deezer", ""
            elif low.startswith("apple-"):
                source, vid = "apple", ""
            elif low.startswith("bandcamp-"):
                source, vid = "bandcamp", ""
            else:
                source, vid = "youtube", key  # старые файлы — голый id видео YouTube
            out.append({
                "key": key, "file": f, "url": "/Fonoteka/" + f,
                "source": source, "vid": vid,
                "size": os.path.getsize(full),
            })
    except OSError:
        pass
    return {"results": out}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def _cors(self):
        # Разрешаем странице, открытой как file:// (или с другого хоста),
        # обращаться к локальному серверу Фонотеки.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200, cb=None):
        # cb (callback) задан → отдаём JSONP (<script>), а не fetch-ответ. Это нужно,
        # потому что в некоторых браузерах fetch к localhost ломается/режется, а
        # загрузка <script> работает (так же, как превью через Apple). Фронт сначала
        # пробует fetch, при сбое — JSONP, и поиск/стрим/скачивание всё равно работают.
        if cb and re.match(r"^[A-Za-z0-9_$.]{1,64}$", cb):
            body = (cb + "(" + json.dumps(obj, ensure_ascii=False) + ");").encode("utf-8")
            ctype = "application/javascript; charset=utf-8"
        else:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/fonoteka.html")
            self.end_headers()
            return
        if p.path == "/fonoteka.html":
            # Анти-кэш: всегда подмешиваем версию из времени изменения файла.
            # Браузер не сможет показать старую версию страницы — при любом
            # изменении fonoteka.html ссылка меняется и грузится свежий код.
            try:
                mtime = str(int(os.path.getmtime(os.path.join(ROOT, "fonoteka.html"))))
            except OSError:
                mtime = "0"
            q = parse_qs(p.query)
            if (q.get("v") or [""])[0] != mtime:
                self.send_response(302)
                self.send_header("Location", "/fonoteka.html?v=" + mtime)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._serve_static()
        if p.path == "/api/search":
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            term = (q.get("q") or [""])[0].strip()
            if not term:
                return self._json({"results": []}, cb=cb)
            src_raw = (q.get("src") or [""])[0].strip()
            sources = [s for s in src_raw.split(",") if s] or None
            return self._json(search(term, sources), cb=cb)
        if p.path == "/api/booksearch":
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            term = (q.get("q") or [""])[0].strip()
            if not term:
                return self._json({"results": []}, cb=cb)
            src_raw = (q.get("src") or [""])[0].strip()
            sources = [s for s in src_raw.split(",") if s] or None
            return self._json(book_search(term, sources), cb=cb)
        if p.path == "/api/library":
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            return self._json(library(), cb=cb)
        if p.path == "/api/stream":
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            url = (q.get("url") or [""])[0].strip()
            if not url:
                return self._json({"error": "no url"}, 400, cb=cb)
            last_err = ""
            for fast, use_cookies in ((True, False), (False, True)):
                u, err = _resolve_stream(url, use_cookies, fast)
                if u:
                    return self._json({"ok": True, "stream": u}, cb=cb)
                last_err = err or last_err
            return self._json({"error": last_err or "не удалось"}, cb=cb)
        if p.path == "/api/download":
            # GET-вариант скачивания (для JSONP-фолбэка, когда fetch/POST не работает)
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            url = (q.get("url") or [""])[0].strip()
            key = (q.get("key") or [""])[0].strip()
            vid = (q.get("id") or [""])[0].strip()
            if not url and vid:
                url = "https://www.youtube.com/watch?v=%s" % vid
            if not key:
                key = _safe_key("youtube", vid or url)
            if not url:
                return self._json({"error": "no url"}, 400, cb=cb)
            return self._json(download(url, key), cb=cb)
        if p.path == "/api/rthls":
            # адрес HLS-потока Rutube (для стриминга аудиокниг «на лету»)
            q = parse_qs(p.query)
            cb = (q.get("callback") or [None])[0]
            vid = (q.get("id") or [""])[0].strip()
            m = _rutube_hls(vid) if vid else ""
            if not m:
                return self._json({"error": "нет потока"}, cb=cb)
            return self._json({"ok": True, "url": "/api/hls?u=" + urllib.parse.quote(m, safe="")}, cb=cb)
        if p.path == "/api/hls":
            # прокси HLS: у Rutube нет CORS, поэтому m3u8 и сегменты гоняем через
            # свой сервер (переписывая ссылки внутри плейлиста на этот же прокси).
            q = parse_qs(p.query)
            u = (q.get("u") or [""])[0]
            if not u:
                return self.send_error(400)
            return self._proxy_hls(u)
        return self._serve_static()

    def _proxy_hls(self, url):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Fonoteka)"})
            r = urllib.request.urlopen(req, timeout=20)
            data = r.read()
            ct = r.headers.get("Content-Type", "")
        except Exception as e:
            return self.send_error(502, str(e)[:120])
        is_m3u8 = url.split("?")[0].lower().endswith(".m3u8") or "mpegurl" in ct.lower()
        if is_m3u8:
            base = url.rsplit("/", 1)[0] + "/"
            def _wrap(link):
                seg = link if link.startswith("http") else urllib.parse.urljoin(base, link)
                return "/api/hls?u=" + urllib.parse.quote(seg, safe="")
            out = []
            for line in data.decode("utf-8", "replace").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(_wrap(s))                       # сегмент или под-плейлист
                elif 'URI="' in s:                             # #EXT-X-KEY / #EXT-X-MEDIA
                    out.append(re.sub(r'URI="([^"]+)"',
                                      lambda m: 'URI="' + _wrap(m.group(1)) + '"', s))
                else:
                    out.append(line)
            body = ("\n".join(out)).encode("utf-8")
            ctype = "application/vnd.apple.mpegurl"
        else:
            body, ctype = data, (ct or "video/mp2t")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _serve_static(self):
        """Отдача файлов с поддержкой HTTP Range (нужно браузеру для стрима
        и перемотки аудио — без этого часть песен не воспроизводится)."""
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().do_GET()
        if not os.path.isfile(path):
            return self.send_error(404, "File not found")
        ctype = self.guess_type(path)
        # Явно проставляем аудио-тип: Python не знает .m4a/.opus и отдаёт их как
        # application/octet-stream, из-за чего часть браузеров не играет их в
        # <audio>. Ставим правильный тип — тогда звук играет везде.
        _ext = os.path.splitext(path)[1].lower()
        _amime = {".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".opus": "audio/ogg",
                  ".ogg": "audio/ogg", ".aac": "audio/aac", ".wav": "audio/wav",
                  ".flac": "audio/flac", ".webm": "audio/webm"}
        if _ext in _amime:
            ctype = _amime[_ext]
        try:
            f = open(path, "rb")
        except OSError:
            return self.send_error(404, "File not found")
        try:
            size = os.fstat(f.fileno()).st_size
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            partial = False
            if rng and rng.strip().lower().startswith("bytes="):
                try:
                    s_s, _, e_s = rng.split("=", 1)[1].partition("-")
                    start = int(s_s) if s_s else 0
                    end = int(e_s) if e_s else size - 1
                    if start < 0 or start >= size or start > end:
                        raise ValueError
                    end = min(end, size - 1)
                    partial = True
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            f.seek(start)
            remaining = length
            try:
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # браузер прервал загрузку (перемотка/смена трека) — это нормально
        finally:
            f.close()

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/download":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return self._json({"error": "bad request"}, 400)
            url = (data.get("url") or "").strip()
            key = (data.get("key") or "").strip()
            vid = (data.get("id") or "").strip()
            if not url and vid:           # обратная совместимость
                url = f"https://www.youtube.com/watch?v={vid}"
            if not key:
                key = _safe_key("youtube", vid or url)
            if not url:
                return self._json({"error": "no url"}, 400)
            return self._json(download(url, key))
        self.send_error(404)

    def log_message(self, *a):
        pass  # тихий лог

class DualStackServer(ThreadingHTTPServer):
    """Слушаем И IPv4 (127.0.0.1), И IPv6 (::1) на одном сокете.
    Браузеры резолвят 'localhost' часто в ::1 — если сервер только на
    127.0.0.1, запрос к ::1 отвергается и поиск «недоступен». Двойной стек
    лечит это: localhost подключается, какой бы адрес браузер ни выбрал."""
    address_family = socket.AF_INET6
    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()

if __name__ == "__main__":
    print("=" * 44)
    print("  FONOTEKA server  ->  http://localhost:%d" % PORT)
    print("  Music folder:", MUSIC_DIR)
    print("  Keep this window open while listening.")
    print("=" * 44)
    try:
        srv = DualStackServer(("::", PORT), Handler)
    except OSError:
        # на случай, если IPv6 в системе выключен — откатываемся на IPv4
        srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.serve_forever()
