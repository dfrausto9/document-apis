"""Project Gutenberg straight from the source: the official catalog file.

Project Gutenberg publishes its whole catalog as one CSV (about 75,000 books) and asks
programs to use it instead of crawling the website. We download it once at startup,
refresh it once a day and search it in memory, so searches are instant and never depend
on a third-party API. Before serving a book we read its own metadata file (RDF) to
confirm it is "Public domain in the USA"; the few copyrighted titles are left out.
"""
import asyncio
import csv
import gzip
import io
import logging
import os
import re
import sys
import time
import unicodedata

import httpx

log = logging.getLogger("bookfinder.gutenberg")

PG = os.getenv("GUTENBERG_MIRROR", "https://www.gutenberg.org").rstrip("/")
CATALOG_URLS = [f"{PG}/cache/epub/feeds/pg_catalog.csv.gz", f"{PG}/cache/epub/feeds/pg_catalog.csv"]
REFRESH_SECONDS = 24 * 3600

_books: list[dict] = []
_by_id: dict[int, tuple] = {}
_loaded_at = 0.0
_lock = asyncio.Lock()
_rights: dict[int, tuple[float, bool]] = {}  # id -> (checked_at, is_public_domain)

def _fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(s))


def _parse_person(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    role = None
    m = re.search(r"\[([^\]]+)\]\s*$", raw)
    if m:
        role, raw = m.group(1).lower(), raw[: m.start()].strip()
    born = died = None
    m = re.search(r",\s*(\d{1,4})?\??\s*(BCE)?\s*-\s*(\d{1,4})?\??\s*(BCE)?\s*$", raw)
    if m:
        born = int(m.group(1)) * (-1 if m.group(2) else 1) if m.group(1) else None
        died = int(m.group(3)) * (-1 if m.group(4) else 1) if m.group(3) else None
        raw = raw[: m.start()].strip()
    return {"name": raw, "born": born, "died": died, "role": role}


def _haystack(s: str) -> str:
    return " " + " ".join(_words(s)) + " "


def parse_catalog(text: str) -> list[tuple]:
    """Keep each book as one compact tuple; people and subjects are parsed only for results.
    (gid, title, languages, authors_raw, subjects_raw, title_hay, people_hay, topics_hay)"""
    books = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row.get("Type") or "").strip() != "Text":
            continue
        try:
            gid = int(row["Text#"])
        except (KeyError, ValueError):
            continue
        title = " ".join((row.get("Title") or "").split())
        authors = row.get("Authors") or ""
        subjects = row.get("Subjects") or ""
        topics = subjects + " ; " + (row.get("Bookshelves") or "")
        people_words = re.sub(r"\[[^\]]*\]|\d+|BCE", " ", authors)
        books.append((
            gid,
            sys.intern(title) if len(title) < 40 else title,
            sys.intern((row.get("Language") or "").replace(" ", "")),
            authors,
            subjects,
            _haystack(title),
            _haystack(people_words),
            _haystack(topics),
        ))
    return books


async def load_catalog(http: httpx.AsyncClient, force: bool = False) -> None:
    global _books, _by_id, _loaded_at
    if _books and not force and time.time() - _loaded_at < REFRESH_SECONDS:
        return
    async with _lock:
        if _books and not force and time.time() - _loaded_at < REFRESH_SECONDS:
            return
        last = None
        for url in CATALOG_URLS:
            try:
                r = await http.get(url, timeout=httpx.Timeout(90.0, connect=10.0))
                r.raise_for_status()
                raw = gzip.decompress(r.content) if url.endswith(".gz") else r.content
                books = await asyncio.to_thread(parse_catalog, raw.decode("utf-8", errors="replace"))
                if len(books) < 1000:
                    raise RuntimeError(f"catalog looked too small ({len(books)} books)")
                _books, _by_id, _loaded_at = books, {b[0]: b for b in books}, time.time()
                log.warning("Gutenberg catalog loaded: %d books from %s", len(books), url)
                return
            except Exception as e:  # try the next copy
                last = f"{url}: {type(e).__name__}: {e}"
                log.warning("Gutenberg catalog download failed: %s", last)
        if not _books:
            raise RuntimeError(f"Gutenberg catalog unavailable ({last})")


def catalog_size() -> int:
    return len(_books)


def _has(hay: str, w: str) -> bool:
    return f" {w}" in hay  # word, or the start of a word ("tao" matches "taoism")


def _score(b: tuple, q: list[str], a: list[str]) -> int:
    _, title, _, _, _, t_hay, p_hay, s_hay = b
    if a and not all(_has(p_hay, w) for w in a):
        return 0
    if not q:
        return 5
    if all(_has(t_hay, w) or _has(p_hay, w) for w in q):
        score = 100 + 10 * sum(_has(t_hay, w) for w in q) + 3 * sum(_has(p_hay, w) for w in q)
        if f" {' '.join(q)} " in t_hay:
            score += 50  # the whole phrase is in the title
        return score
    if all(_has(t_hay, w) or _has(p_hay, w) or _has(s_hay, w) for w in q):
        return 40 + 5 * sum(_has(t_hay, w) for w in q)
    return 0


def search(q: str, author: str | None, language: str | None, limit: int) -> list[tuple]:
    qw, aw = _words(q), _words(author or "")
    scored = []
    for b in _books:
        if language and language not in b[2].split(";"):
            continue
        s = _score(b, qw, aw)
        if s:
            scored.append((s, -len(b[1]), b))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [x[2] for x in scored[: limit * 3]]


def get(gid: int) -> tuple | None:
    return _by_id.get(gid)


async def is_public_domain(http: httpx.AsyncClient, gid: int) -> bool:
    hit = _rights.get(gid)
    if hit and time.time() - hit[0] < 7 * 24 * 3600:
        return hit[1]
    r = await http.get(f"{PG}/cache/epub/{gid}/pg{gid}.rdf")
    r.raise_for_status()
    m = re.search(r"<dcterms:rights>(.*?)</dcterms:rights>", r.text, re.S)
    ok = bool(m and "public domain" in m.group(1).lower())
    _rights[gid] = (time.time(), ok)
    return ok


def to_result(b: tuple) -> dict:
    gid, title, langs, authors_raw, subjects_raw = b[:5]
    people = [p for p in (_parse_person(x) for x in authors_raw.split(";")) if p]
    authors = [{"name": p["name"], "born": p["born"], "died": p["died"]}
               for p in people if p["role"] in (None, "author", "creator")]
    return {
        "id": f"gutenberg:{gid}",
        "source": "Project Gutenberg",
        "kind": "text",
        "title": title,
        "authors": authors,
        "translators": [p["name"] for p in people if p["role"] == "translator"],
        "languages": [l for l in langs.split(";") if l],
        "year": None,
        "subjects": [x.strip() for x in subjects_raw.split(";") if x.strip()][:9],
        "summary": "",
        "cover": f"{PG}/cache/epub/{gid}/pg{gid}.cover.medium.jpg",
        "read_online": f"{PG}/ebooks/{gid}.html.images",
        "clean_reader": f"/read/gutenberg:{gid}",
        "downloads": {
            "epub": f"{PG}/ebooks/{gid}.epub3.images",
            "kindle": f"{PG}/ebooks/{gid}.kf8.images",
            "html": f"{PG}/ebooks/{gid}.html.images",
            "txt": f"{PG}/ebooks/{gid}.txt.utf-8",
        },
        "popularity": None,
        "rights": "Public domain in the USA (Project Gutenberg)",
    }
