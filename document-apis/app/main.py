"""Public Domain Book Finder API

Search 3 free, legal libraries at once (Project Gutenberg, Open Library / Internet Archive,
LibriVox) and read public-domain books as clean text: no ads, no pop-ups, no sign-in.
"""
import asyncio
import os
import re
import unicodedata

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import sources
from .cache import TTLCache
from .textclean import clean_book

app = FastAPI(
    title="Public Domain Book Finder",
    version="1.0.0",
    description="Find free, legal, public-domain books and audiobooks across 3 libraries, "
                "and read them as clean, ad-free text.",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

PROXY_SECRET = os.getenv("RAPIDAPI_PROXY_SECRET", "")
OPEN_PATHS = {"/", "/health", "/docs", "/openapi.json", "/redoc"}

search_cache = TTLCache(ttl_seconds=6 * 3600, max_items=800)
book_cache = TTLCache(ttl_seconds=24 * 3600, max_items=800)
text_cache = TTLCache(ttl_seconds=24 * 3600, max_items=40)  # whole books are big; keep fewer

SOURCES = {
    "gutenberg": sources.search_gutenberg,
    "openlibrary": sources.search_openlibrary,
    "librivox": sources.search_librivox,
}


@app.middleware("http")
async def rapidapi_guard(request: Request, call_next):
    """When RAPIDAPI_PROXY_SECRET is set, only requests coming through RapidAPI are allowed."""
    if PROXY_SECRET and request.url.path not in OPEN_PATHS:
        if request.headers.get("X-RapidAPI-Proxy-Secret") != PROXY_SECRET:
            return JSONResponse({"error": "Please call this API through RapidAPI."}, status_code=403)
    return await call_next(request)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.split(r"[:;(\[]", s)[0].strip()
    s = re.sub(r",\s*(the|a|an)$", "", s)  # "Sample Way, The"
    s = re.sub(r"^(the|a|an)\s+", "", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def _surname(book: dict) -> str:
    if not book["authors"]:
        return ""
    name = book["authors"][0]["name"] or ""
    last = name.split(",")[0] if "," in name else (name.split() or [""])[-1]
    return _norm(last)


def merge_results(groups: list[list[dict]]) -> list[dict]:
    """Interleave the sources and fold duplicates (same title + author) into one result.
    The first copy found becomes the main entry; the others are listed under also_available."""
    merged: dict[str, dict] = {}
    order: list[str] = []
    longest = max((len(g) for g in groups), default=0)
    for i in range(longest):
        for g in groups:
            if i >= len(g):
                continue
            b = g[i]
            key = f"{_norm(b['title'])}|{_surname(b)}"
            if key in merged:
                main = merged[key]
                main.setdefault("also_available", []).append(
                    {"id": b["id"], "source": b["source"], "kind": b["kind"], "read_online": b["read_online"]}
                )
                if not main.get("cover") and b.get("cover"):
                    main["cover"] = b["cover"]
            else:
                merged[key] = dict(b)
                order.append(key)
    out = []
    for k in order:
        b = merged[k]
        b.setdefault("also_available", [])
        out.append(b)
    return out


@app.get("/")
async def root():
    return {
        "name": "Public Domain Book Finder",
        "endpoints": {
            "/search": "Search 3 free libraries at once. ?q=tao te ching&type=all&limit=9",
            "/book/{id}": "Details and every free format for one book, e.g. /book/gutenberg:216",
            "/read/{id}": "Clean, ad-free reading pages, e.g. /read/gutenberg:216?page=1",
        },
        "sources": ["Project Gutenberg", "Open Library / Internet Archive (public access only)", "LibriVox"],
    }


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/search")
async def search(
    q: str = Query("", max_length=200, description="Title, keywords or topic, e.g. 'jaguar myths'"),
    author: str | None = Query(None, max_length=120, description="Author name, e.g. 'Lao Tzu'"),
    language: str | None = Query(None, max_length=20, description="Language code or name: en, es, fr…"),
    type: str = Query("all", pattern="^(all|text|audio)$", description="text = books, audio = audiobooks"),
    source: str = Query("all", description="all, or any of: gutenberg, openlibrary, librivox (comma separated)"),
    limit: int = Query(9, ge=1, le=30, description="Results to return (default 9)"),
):
    q = q.strip()
    if not q and not (author and author.strip()):
        raise HTTPException(400, "Add a search with q= (title or topic) or author=.")

    wanted = list(SOURCES) if source == "all" else [s.strip().lower() for s in source.split(",") if s.strip()]
    bad = [s for s in wanted if s not in SOURCES]
    if bad:
        raise HTTPException(400, f"Unknown source: {', '.join(bad)}. Use gutenberg, openlibrary or librivox.")
    if type == "audio":
        wanted = [s for s in wanted if s == "librivox"]
    elif type == "text":
        wanted = [s for s in wanted if s != "librivox"]
    if not wanted:
        raise HTTPException(400, "That type and source don't match (audiobooks only come from librivox).")

    key = f"{q.lower()}|{(author or '').lower()}|{language}|{type}|{','.join(wanted)}|{limit}"
    cached = search_cache.get(key)
    if cached:
        return cached

    status: dict[str, str] = {}
    async with sources.client() as http:
        tasks = [SOURCES[s](http, q, author, language, limit) for s in wanted]
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    groups = []
    for name, res in zip(wanted, outcomes):
        if isinstance(res, Exception):
            status[name] = "unavailable"
            groups.append([])
        else:
            status[name] = f"{len(res)} found"
            groups.append(res)

    if all(v == "unavailable" for v in status.values()):
        raise HTTPException(503, "The free libraries didn't answer just now. Please try again in a minute.")

    results = merge_results(groups)[:limit]
    body = {"query": {"q": q, "author": author, "language": language, "type": type},
            "count": len(results), "sources": status, "results": results}
    if "unavailable" not in status.values():
        search_cache.set(key, body)
    return body


def _split_id(book_id: str) -> tuple[str, str]:
    if ":" not in book_id:
        raise HTTPException(400, "Use an id from /search, like gutenberg:216 or librivox:419.")
    src, raw = book_id.split(":", 1)
    if src not in SOURCES or not raw:
        raise HTTPException(400, "Unknown id. Ids start with gutenberg:, openlibrary: or librivox:.")
    return src, raw


async def _lookup(src: str, raw: str) -> dict:
    cached = book_cache.get(f"{src}:{raw}")
    if cached:
        return cached
    try:
        async with sources.client() as http:
            if src == "gutenberg":
                if not raw.isdigit():
                    raise HTTPException(400, "Gutenberg ids are numbers, like gutenberg:216.")
                book = await sources.get_gutenberg(http, int(raw))
            elif src == "openlibrary":
                book = await sources.get_openlibrary(http, raw)
            else:
                book = await sources.get_librivox(http, raw)
    except httpx.HTTPError:
        raise HTTPException(503, "That library didn't answer just now. Please try again in a minute.")
    if book and book.get("_copyrighted"):
        raise HTTPException(451, "This title is still under copyright, so it isn't offered here.")
    if not book:
        raise HTTPException(404, "No free, public-domain book found with that id.")
    book_cache.set(f"{src}:{raw}", book)
    return book


@app.get("/book/{book_id}")
async def book(book_id: str):
    src, raw = _split_id(book_id)
    return await _lookup(src, raw)


@app.get("/read/{book_id}")
async def read(
    book_id: str,
    page: int = Query(1, ge=1, description="Page number"),
    words_per_page: int = Query(900, ge=200, le=5000, description="About how many words per page"),
    format: str = Query("paragraphs", pattern="^(paragraphs|text)$", description="paragraphs (list) or text (one string)"),
):
    src, raw = _split_id(book_id)
    if src != "gutenberg":
        info = await _lookup(src, raw)
        raise HTTPException(
            422,
            f"The clean reader works with Project Gutenberg books. Read this one free at {info['read_online']}",
        )
    info = await _lookup(src, raw)

    cleaned = text_cache.get(f"{raw}|{words_per_page}")
    if cleaned is None:
        try:
            async with sources.client() as http:
                text = await sources.fetch_gutenberg_text(http, int(raw), info["downloads"].get("txt"))
        except RuntimeError:
            raise HTTPException(503, "Couldn't download that book's text just now. Please try again soon.")
        cleaned = clean_book(text, words_per_page)
        text_cache.set(f"{raw}|{words_per_page}", cleaned)

    pages = cleaned["pages"]
    total = len(pages)
    if page > total:
        raise HTTPException(404, f"This book has {total} pages.")
    content = pages[page - 1]
    return {
        "id": book_id,
        "title": info["title"],
        "authors": [a["name"] for a in info["authors"]],
        "page": page,
        "total_pages": total,
        "word_count": cleaned["word_count"],
        "chapters": cleaned["chapters"],
        "content": content if format == "paragraphs" else "\n\n".join(content),
        "next_page": page + 1 if page < total else None,
        "rights": info["rights"],
    }
