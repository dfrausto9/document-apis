"""The three free, legal sources. Each one returns books in the same shape.

- Project Gutenberg (its official catalog file), public-domain titles only
- Open Library / Internet Archive, "public" full-access scans only (no lending-library books)
- LibriVox, public-domain audiobooks read by volunteers
"""
import asyncio
import os
import re
from html import unescape

import httpx

from . import gutenberg

OPENLIB = "https://openlibrary.org"
LIBRIVOX = "https://librivox.org/api/feed/audiobooks"
GUTENBERG_FILES = os.getenv("GUTENBERG_MIRROR", "https://www.gutenberg.org")

_contact = os.getenv("CONTACT_EMAIL", "")
USER_AGENT = "PublicDomainBookFinder/1.0" + (f" ({_contact})" if _contact else "")
TIMEOUT = httpx.Timeout(float(os.getenv("SOURCE_TIMEOUT", "12")), connect=6.0)

_TAG = re.compile(r"<[^>]+>")


def _plain(html: str | None, limit: int = 600) -> str:
    if not html:
        return ""
    text = re.sub(r"\s+", " ", unescape(_TAG.sub(" ", html))).strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _int(v):
    try:
        return int(str(v).split()[-1])
    except (TypeError, ValueError, IndexError):
        return None


def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


# ---------------- Project Gutenberg ----------------

_LANG_NAMES = {"english": "en", "spanish": "es", "french": "fr", "german": "de", "italian": "it",
               "portuguese": "pt", "dutch": "nl", "latin": "la", "chinese": "zh", "japanese": "ja"}


def lang_code(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.strip().lower()
    return _LANG_NAMES.get(lang, lang[:2] if len(lang) > 2 else lang)


async def search_gutenberg(http: httpx.AsyncClient, q: str, author: str | None, language: str | None, limit: int):
    await gutenberg.load_catalog(http)
    candidates = await asyncio.to_thread(gutenberg.search, q, author, lang_code(language), limit)
    out = []
    # confirm public-domain status from each book's own metadata, a few at a time
    for i in range(0, len(candidates), 6):
        if len(out) >= limit:
            break
        batch = candidates[i:i + 6]
        checks = await asyncio.gather(*(gutenberg.is_public_domain(http, b[0]) for b in batch),
                                      return_exceptions=True)
        for b, ok in zip(batch, checks):
            if ok is True:
                out.append(gutenberg.to_result(b))
    return out[:limit]


async def get_gutenberg(http: httpx.AsyncClient, gid: int) -> dict | None:
    await gutenberg.load_catalog(http)
    b = gutenberg.get(gid)
    if not b:
        return None
    if not await gutenberg.is_public_domain(http, gid):
        return {"_copyrighted": True}
    return gutenberg.to_result(b)


async def fetch_gutenberg_text(http: httpx.AsyncClient, gid: int, txt_url: str | None) -> str:
    urls = [u for u in (txt_url,) if u] + [
        f"{GUTENBERG_FILES}/cache/epub/{gid}/pg{gid}.txt",
        f"{GUTENBERG_FILES}/files/{gid}/{gid}-0.txt",
    ]
    last = None
    for url in urls:
        try:
            r = await http.get(url)
            if r.status_code == 200 and r.content.strip():
                try:
                    return r.content.decode("utf-8")
                except UnicodeDecodeError:
                    return r.content.decode("latin-1")
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:  # try the next copy
            last = str(e)
    raise RuntimeError(f"Could not download the text ({last})")


# ---------------- Open Library / Internet Archive ----------------

_OL_FIELDS = "key,title,author_name,first_publish_year,ia,ebook_access,language,cover_i,subject,public_scan_b"


def normalize_openlibrary(d: dict) -> dict | None:
    ia = (d.get("ia") or [None])[0]
    if not ia or d.get("ebook_access") != "public":
        return None
    cover = d.get("cover_i")
    return {
        "id": f"openlibrary:{d.get('key', '').split('/')[-1]}",
        "source": "Open Library / Internet Archive",
        "kind": "text",
        "title": (d.get("title") or "").strip(),
        "authors": [{"name": n, "born": None, "died": None} for n in d.get("author_name", [])],
        "translators": [],
        "languages": d.get("language", [])[:6],
        "year": d.get("first_publish_year"),
        "subjects": (d.get("subject") or [])[:9],
        "summary": "",
        "cover": f"https://covers.openlibrary.org/b/id/{cover}-L.jpg" if cover else None,
        "read_online": f"https://archive.org/details/{ia}",
        "clean_reader": None,
        "downloads": {"all_formats": f"https://archive.org/download/{ia}"},
        "popularity": None,
        "rights": "Full public access on Internet Archive (no borrowing needed)",
        "_ia": ia,
    }


_OL_LANG3 = {"en": "eng", "es": "spa", "fr": "fre", "de": "ger", "it": "ita", "pt": "por",
             "nl": "dut", "la": "lat", "zh": "chi", "ja": "jpn"}


async def search_openlibrary(http: httpx.AsyncClient, q: str, author: str | None, language: str | None, limit: int):
    params = {"ebook_access": "public", "limit": max(limit * 2, 10), "fields": _OL_FIELDS}
    if q:
        params["q"] = q
    if author:
        params["author"] = author
    code = lang_code(language)
    if code:
        params["language"] = _OL_LANG3.get(code, code)
    r = await http.get(f"{OPENLIB}/search.json", params=params)
    r.raise_for_status()
    out = []
    for d in r.json().get("docs", []):
        n = normalize_openlibrary(d)
        if n:
            n.pop("_ia", None)
            out.append(n)
    return out[:limit]


async def get_openlibrary(http: httpx.AsyncClient, work_id: str) -> dict | None:
    if not re.fullmatch(r"OL\d+[WM]", work_id):
        return None
    field = "key" if work_id.endswith("W") else "edition_key"
    q = f"/works/{work_id}" if field == "key" else work_id
    r = await http.get(f"{OPENLIB}/search.json",
                       params={"q": f'{field}:"{q}"', "ebook_access": "public", "fields": _OL_FIELDS, "limit": 1})
    r.raise_for_status()
    docs = r.json().get("docs", [])
    n = normalize_openlibrary(docs[0]) if docs else None
    if n:
        n.pop("_ia", None)
    return n


# ---------------- LibriVox ----------------

def _lv_person(p: dict) -> str:
    return " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x).strip()


def normalize_librivox(b: dict) -> dict:
    lid = b.get("id")
    chapters = [
        {"title": s.get("title"), "listen": s.get("listen_url"), "seconds": _int(s.get("playtime"))}
        for s in (b.get("sections") or [])
    ]
    return {
        "id": f"librivox:{lid}",
        "source": "LibriVox",
        "kind": "audio",
        "title": unescape(b.get("title") or "").strip(),
        "authors": [
            {"name": _lv_person(a), "born": _int(a.get("dob")), "died": _int(a.get("dod"))}
            for a in b.get("authors", [])
        ],
        "translators": [_lv_person(t) for t in (b.get("translators") or []) if _lv_person(t)],
        "languages": [b.get("language")] if b.get("language") else [],
        "year": _int(b.get("copyright_year")),
        "subjects": [g.get("name", "").lstrip("*") for g in (b.get("genres") or [])][:9],
        "summary": _plain(b.get("description")),
        "cover": None,
        "read_online": b.get("url_librivox") or b.get("url_iarchive") or None,
        "clean_reader": None,
        "downloads": {k: v for k, v in {
            "mp3_zip": b.get("url_zip_file"),
            "podcast_rss": b.get("url_rss"),
            "text_source": b.get("url_text_source"),
        }.items() if v},
        "audio": {"total_time": b.get("totaltime"), "chapters": chapters} if chapters or b.get("totaltime") else None,
        "popularity": None,
        "rights": "Public domain audiobook (LibriVox)",
    }


async def search_librivox(http: httpx.AsyncClient, q: str, author: str | None, language: str | None, limit: int):
    params = {"format": "json", "limit": limit, "extended": 1}
    if q:
        params["title"] = q
    if author:
        params["author"] = author.split()[-1]
    r = await http.get(LIBRIVOX, params=params)
    if r.status_code == 404:  # LibriVox answers 404 when nothing matches
        return []
    r.raise_for_status()
    data = r.json()
    books = data.get("books", []) if isinstance(data, dict) else []
    out = [normalize_librivox(b) for b in books]
    if language:
        code = lang_code(language)
        out = [b for b in out if any(lang_code(l) == code for l in b["languages"])]
    return out[:limit]


async def get_librivox(http: httpx.AsyncClient, lid: str) -> dict | None:
    if not lid.isdigit():
        return None
    r = await http.get(LIBRIVOX, params={"id": lid, "format": "json", "extended": 1})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    books = r.json().get("books", [])
    return normalize_librivox(books[0]) if books else None
