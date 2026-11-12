"""Clean PDF Reader API.

A FastAPI router for the document-apis service. Each endpoint accepts a PDF by
URL (GET ?url=... or a POST form field) or as an uploaded file (POST multipart
field "file").

    /pdf/read      clean, readable text, served page by page
    /pdf/chapters  chapter list from the PDF outline or detected headings,
                   plus the text of one chapter
    /pdf/info      metadata (title, author, dates, page size) and tables

Wire it into main.py with:

    from pdf_reader import router as pdf_router
    app.include_router(pdf_router)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import math
import re
import socket
import statistics
import time
import unicodedata
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx
import pdfplumber
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/pdf", tags=["Clean PDF Reader"])

MAX_BYTES = 25 * 1024 * 1024  # 25 MB
MAX_PAGES = 400
MAX_TABLE_PAGES = 50
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
CACHE_SIZE = 8
URL_CACHE_TTL = 600  # seconds
USER_AGENT = "CleanPDFReader/1.0 (+https://rapidapi.com/RecipeScraperPro)"

# Parsing is CPU and memory heavy, so only a couple run at once.
_parse_slots = asyncio.Semaphore(2)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


# --------------------------------------------------------------------------
# Getting the PDF bytes
# --------------------------------------------------------------------------

def _check_public_host(url: str) -> None:
    """Refuse URLs that point at private, loopback or internal addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _fail(400, "bad_url", "url must be a full http:// or https:// link.")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise _fail(400, "bad_url", f"Could not find the host {parsed.hostname}.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise _fail(400, "bad_url", "That address is not allowed.")


async def _fetch_pdf(url: str) -> bytes:
    current = url
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await run_in_threadpool(_check_public_host, current)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise _fail(502, "fetch_failed", "The link redirected without a destination.")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code != 200:
                        raise _fail(502, "fetch_failed", f"The link returned HTTP {resp.status_code}.")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                        raise _fail(413, "too_large", "PDFs can be up to 25 MB.")
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise _fail(413, "too_large", "PDFs can be up to 25 MB.")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.TimeoutException:
                raise _fail(504, "fetch_timeout", "The link took too long to respond.")
            except httpx.HTTPError:
                raise _fail(502, "fetch_failed", "Could not download the PDF from that link.")
    raise _fail(502, "fetch_failed", "The link redirected too many times.")


async def _read_upload(file: UploadFile) -> bytes:
    chunks, size = [], 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BYTES:
            raise _fail(413, "too_large", "PDFs can be up to 25 MB.")
        chunks.append(chunk)
    return b"".join(chunks)


def _check_is_pdf(data: bytes) -> None:
    if not data:
        raise _fail(400, "empty_file", "The PDF is empty.")
    if b"%PDF" not in data[:1024]:
        raise _fail(415, "not_a_pdf", "That file is not a PDF.")


# --------------------------------------------------------------------------
# Parsed document model
# --------------------------------------------------------------------------

@dataclass
class Block:
    kind: str  # "heading" or "para"
    text: str
    page: int  # 1-based source page
    size: float


@dataclass
class Chapter:
    title: str
    block_start: int
    level: int = 1


@dataclass
class ParsedDoc:
    sha: str
    page_count: int
    metadata: dict
    page_size: dict | None
    blocks: list[Block]
    chapters: list[Chapter]
    chapter_source: str
    stats: dict
    tables: dict = field(default_factory=dict)  # cache keyed by page range

    @property
    def title(self) -> str | None:
        return self.metadata.get("title")


_doc_cache: "OrderedDict[str, ParsedDoc]" = OrderedDict()
_url_cache: dict[str, tuple[str, float]] = {}


def _cache_put(doc: ParsedDoc) -> None:
    _doc_cache[doc.sha] = doc
    _doc_cache.move_to_end(doc.sha)
    while len(_doc_cache) > CACHE_SIZE:
        _doc_cache.popitem(last=False)


# --------------------------------------------------------------------------
# Text cleaning
# --------------------------------------------------------------------------

_PAGE_NUM = re.compile(
    r"^(?:page\s*)?[-–—]?\s*(?:\d{1,4}|[ivxlcdm]{1,7})\s*[-–—]?(?:\s*(?:of|/)\s*\d{1,4})?$",
    re.IGNORECASE,
)
_CHAPTER_WORD = re.compile(
    r"^(?:chapter|part|book|section|prologue|epilogue|introduction|preface|appendix)\b"
    r"(?:\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)\b)?",
    re.IGNORECASE,
)
_END_PUNCT = tuple('.!?:;"”’)')
EDGE_LINES = 2
# Word gaps are measured relative to font size. A fixed 3pt gap glues words
# together in tightly set PDFs (LaTeX papers, justified text).
X_TOL_RATIO = 0.15


def _upright(page):
    """Drop rotated/vertical text such as figure labels; it only adds noise."""
    return page.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))


def _norm_repeat(text: str) -> str:
    return re.sub(r"\d+", "#", text.lower()).strip()


def _clean_chars(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("­", "")  # soft hyphen
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text).strip()


def _line_size(line: dict) -> float:
    sizes = [c.get("size", 0) for c in line.get("chars", []) if c.get("text", "").strip()]
    return round(statistics.median(sizes), 1) if sizes else 0.0


def _is_heading(text: str, size: float, body: float) -> bool:
    if len(text) > 120 or not text:
        return False
    if body and size >= body * 1.2:
        return True
    return bool(_CHAPTER_WORD.match(text)) and len(text) <= 80 and not text.endswith((".", ","))


def _join(prev: str, nxt: str, stats: Counter) -> str:
    if (
        len(prev) > 1
        and prev.endswith("-")
        and prev[-2].isalpha()
        and nxt[:1].islower()
    ):
        stats["hyphens_fixed"] += 1
        return prev[:-1] + nxt
    stats["lines_joined"] += 1
    return f"{prev} {nxt}"


def _extract_blocks(pdf) -> tuple[list[Block], Counter, float]:
    stats: Counter = Counter()
    raw_pages: list[list[dict]] = []
    all_sizes: list[float] = []

    for raw_page in pdf.pages:
        page = _upright(raw_page)
        lines = []
        for ln in page.extract_text_lines(
            layout=False, strip=True, return_chars=True, x_tolerance_ratio=X_TOL_RATIO
        ):
            text = _clean_chars(ln["text"])
            if not text:
                continue
            size = _line_size(ln)
            lines.append({"text": text, "top": ln["top"], "bottom": ln["bottom"],
                          "x0": ln["x0"], "x1": ln["x1"], "size": size})
            all_sizes.extend([size] * max(1, len(text) // 20))
        raw_pages.append(lines)
        raw_page.flush_cache()

    body = statistics.median(all_sizes) if all_sizes else 0.0

    # Repeated running headers and footers.
    n = len(raw_pages)
    repeated: set[str] = set()
    if n >= 3:
        counts: Counter = Counter()
        for lines in raw_pages:
            edge = lines[:EDGE_LINES] + lines[-EDGE_LINES:]
            counts.update({_norm_repeat(l["text"]) for l in edge})
        threshold = max(2, math.ceil(n * 0.4))
        repeated = {k for k, c in counts.items() if c >= threshold and k}

    blocks: list[Block] = []
    gaps: list[float] = []
    kept_pages: list[list[dict]] = []
    for lines in raw_pages:
        kept = []
        for i, l in enumerate(lines):
            at_edge = i < EDGE_LINES or i >= len(lines) - EDGE_LINES
            big = body and l["size"] >= body * 1.15
            if at_edge and not big:
                if _PAGE_NUM.match(l["text"]):
                    stats["page_numbers"] += 1
                    continue
                if _norm_repeat(l["text"]) in repeated:
                    stats["headers_footers"] += 1
                    continue
            kept.append(l)
        for a, b in zip(kept, kept[1:]):
            g = b["top"] - a["bottom"]
            if g > 0:
                gaps.append(g)
        kept_pages.append(kept)

    line_gap = statistics.median(gaps) if gaps else 2.0

    for page_no, lines in enumerate(kept_pages, start=1):
        if not lines:
            continue
        margin = Counter(round(l["x0"]) for l in lines).most_common(1)[0][0]
        right = max(l["x1"] for l in lines)
        width = max(1.0, right - margin)
        current: list[dict] = []

        def flush():
            if not current:
                return
            text = current[0]["text"]
            for nxt in current[1:]:
                text = _join(text, nxt["text"], stats)
            size = current[0]["size"]
            kind = "heading" if _is_heading(text, size, body) else "para"
            blocks.append(Block(kind, text, page_no, size))
            current.clear()

        for l in lines:
            if current:
                prev = current[-1]
                gap = l["top"] - prev["bottom"]
                short = prev["x1"] < margin + width * 0.85
                new_para = (
                    gap > line_gap * 1.6 + 2
                    or l["x0"] > margin + 8
                    or (short and prev["text"].endswith(_END_PUNCT))
                    or abs(l["size"] - prev["size"]) > max(1.0, body * 0.1)
                    or _is_heading(prev["text"], prev["size"], body)
                )
                if new_para:
                    flush()
            current.append(l)
        flush()

    # Paragraphs that continue onto the next page.
    merged: list[Block] = []
    for b in blocks:
        if (
            merged
            and b.kind == "para"
            and merged[-1].kind == "para"
            and merged[-1].page == b.page - 1
            and not merged[-1].text.endswith(_END_PUNCT)
            and b.text[:1].islower()
        ):
            merged[-1].text = _join(merged[-1].text, b.text, stats)
        else:
            merged.append(b)
    return merged, stats, body


# --------------------------------------------------------------------------
# Chapters
# --------------------------------------------------------------------------

def _outline_chapters(pdf, blocks: list[Block]) -> list[Chapter]:
    from pdfminer.pdftypes import resolve1
    from pdfminer.psparser import PSLiteral

    try:
        outlines = list(pdf.doc.get_outlines())
    except Exception:
        return []
    if not outlines:
        return []

    page_ids = {p.page_obj.pageid: i + 1 for i, p in enumerate(pdf.pages)}

    def page_of(dest, action) -> int | None:
        try:
            if dest is None and action:
                action = resolve1(action)
                if isinstance(action, dict):
                    dest = action.get("D")
            dest = resolve1(dest)
            if isinstance(dest, (bytes, str, PSLiteral)):
                name = dest.name if isinstance(dest, PSLiteral) else dest
                dest = resolve1(pdf.doc.get_dest(name))
            if isinstance(dest, dict):
                dest = resolve1(dest.get("D"))
            if isinstance(dest, list) and dest:
                ref = dest[0]
                return page_ids.get(getattr(ref, "objid", None))
        except Exception:
            return None
        return None

    entries = []
    for level, title, dest, action, _ in outlines:
        page = page_of(dest, action)
        title = _clean_chars(title or "")
        if page and title:
            entries.append((level, title, page))
    if not entries:
        return []

    # Use the shallowest outline level that has at least 2 entries.
    by_level = Counter(e[0] for e in entries)
    level = min((lv for lv, c in by_level.items() if c >= 2), default=min(by_level))
    chosen = [e for e in entries if e[0] == level]

    chapters = []
    for _, title, page in chosen:
        on_page = [i for i, b in enumerate(blocks) if b.page == page]
        if not on_page:
            later = [i for i, b in enumerate(blocks) if b.page > page]
            if not later:
                continue
            on_page = [later[0]]
        key = re.sub(r"\W+", " ", title.lower()).strip()
        start = next(
            (i for i in on_page if key and key in re.sub(r"\W+", " ", blocks[i].text.lower())),
            on_page[0],
        )
        chapters.append(Chapter(title, start, level))
    return chapters


def _drop_stacked(chapters: list[Chapter]) -> list[Chapter]:
    """A title followed directly by a subtitle is one chapter start, not two."""
    out: list[Chapter] = []
    for ch in chapters:
        if out and ch.block_start == out[-1].block_start + 1:
            continue
        out.append(ch)
    return out


def _heading_chapters(blocks: list[Block]) -> list[Chapter]:
    heads = [(i, b) for i, b in enumerate(blocks) if b.kind == "heading"]
    named = _drop_stacked([Chapter(b.text, i) for i, b in heads if _CHAPTER_WORD.match(b.text)])
    if len(named) >= 2:
        return named
    sizes = Counter(round(b.size) for _, b in heads)
    for size in sorted(sizes, reverse=True):
        if sizes[size] >= 2:
            found = _drop_stacked([Chapter(b.text, i) for i, b in heads if round(b.size) == size])
            if len(found) >= 2:
                return found
    return []


def _finalize_chapters(chapters: list[Chapter], blocks: list[Block]) -> list[Chapter]:
    seen, out = set(), []
    for ch in sorted(chapters, key=lambda c: c.block_start):
        if ch.block_start not in seen:
            seen.add(ch.block_start)
            out.append(ch)
    if out and out[0].block_start > 0:
        front_words = sum(len(b.text.split()) for b in blocks[: out[0].block_start])
        if front_words:
            out.insert(0, Chapter("Front matter", 0, out[0].level))
    return out


# --------------------------------------------------------------------------
# Metadata
# --------------------------------------------------------------------------

def _pdf_date(value) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("latin-1", "ignore")
    if not isinstance(value, str):
        return None
    m = re.match(r"D?:?(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?([Zz+\-])?(\d{2})?'?(\d{2})?", value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, sign, oh, om = m.groups()
    try:
        dt = datetime(int(y), int(mo or 1), int(d or 1), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return None
    if sign in ("+", "-") and oh:
        offset = timedelta(hours=int(oh), minutes=int(om or 0))
        dt = dt.replace(tzinfo=timezone(offset if sign == "+" else -offset))
    elif sign in ("Z", "z"):
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _meta_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    text = _clean_chars(str(value))
    return text or None


_JUNK_TITLE = re.compile(r"^(untitled|document\d*|microsoft word - .*|.*\.(docx?|pdf|indd|tex))$", re.I)


def _metadata(pdf, blocks: list[Block]) -> dict:
    raw = pdf.metadata or {}
    title = _meta_text(raw.get("Title"))
    if not title or _JUNK_TITLE.match(title):
        early = [b for b in blocks if b.page <= 2 and b.kind == "heading" and len(b.text) <= 150]
        if early:
            title = max(early, key=lambda b: b.size).text

    return {
        "title": title,
        "author": _meta_text(raw.get("Author")),
        "subject": _meta_text(raw.get("Subject")),
        "keywords": _meta_text(raw.get("Keywords")),
        "creator": _meta_text(raw.get("Creator")),
        "producer": _meta_text(raw.get("Producer")),
        "created": _pdf_date(raw.get("CreationDate")),
        "modified": _pdf_date(raw.get("ModDate")),
    }


# --------------------------------------------------------------------------
# Parsing entry point
# --------------------------------------------------------------------------

def _open(data: bytes):
    try:
        return pdfplumber.open(io.BytesIO(data))
    except Exception as exc:
        detail = f"{type(exc).__name__} {exc!r}".lower()
        if "password" in detail or "encrypt" in detail:
            raise _fail(422, "encrypted_pdf", "This PDF is password protected.")
        raise _fail(422, "unreadable_pdf", "This PDF is damaged or could not be opened.")


def _parse(data: bytes, sha: str) -> ParsedDoc:
    with _open(data) as pdf:
        try:
            page_count = len(pdf.pages)
        except Exception:
            raise _fail(422, "unreadable_pdf", "This PDF is damaged or could not be opened.")
        if page_count > MAX_PAGES:
            raise _fail(413, "too_many_pages", f"PDFs can have up to {MAX_PAGES} pages.")
        try:
            blocks, stats, _ = _extract_blocks(pdf)
        except HTTPException:
            raise
        except Exception:
            raise _fail(422, "unreadable_pdf", "This PDF is damaged or could not be read.")
        if not blocks:
            raise _fail(
                422,
                "no_text_layer",
                "This PDF has no text layer (it is probably a scan). Scanned PDFs are not supported yet.",
            )

        chapters = _outline_chapters(pdf, blocks)
        source = "outline" if len(chapters) >= 2 else ""
        if not source:
            chapters = _heading_chapters(blocks)
            source = "headings" if len(chapters) >= 2 else "none"
        if source == "none":
            chapters = [Chapter("Full document", 0)]
        else:
            chapters = _finalize_chapters(chapters, blocks)

        first = pdf.pages[0]
        page_size = {
            "width_pt": round(float(first.width), 1),
            "height_pt": round(float(first.height), 1),
            "width_in": round(float(first.width) / 72, 2),
            "height_in": round(float(first.height) / 72, 2),
        }
        return ParsedDoc(
            sha=sha,
            page_count=page_count,
            metadata=_metadata(pdf, blocks),
            page_size=page_size,
            blocks=blocks,
            chapters=chapters,
            chapter_source=source,
            stats={k: stats.get(k, 0) for k in ("headers_footers", "page_numbers", "hyphens_fixed", "lines_joined")},
        )


def _parse_tables(data: bytes, first: int, last: int) -> list[dict]:
    out = []
    with _open(data) as pdf:
        for page_no in range(first, min(last, len(pdf.pages)) + 1):
            raw_page = pdf.pages[page_no - 1]
            page = _upright(raw_page)
            for t in page.extract_tables({"text_x_tolerance": 1.5}):
                rows = [[_clean_chars(c or "") for c in row] for row in t if row]
                rows = [r for r in rows if any(r)]
                if len(rows) < 2:
                    continue
                cells = [c for r in rows for c in r]
                if sum(1 for c in cells if c) / len(cells) < 0.4:
                    continue  # mostly empty grid: a figure or layout box, not a table
                out.append({
                    "page": page_no,
                    "rows": len(rows),
                    "columns": max(len(r) for r in rows),
                    "data": rows,
                })
            raw_page.flush_cache()
    return out


async def _load(url: str | None, file: UploadFile | None, need_bytes: bool = False):
    """Return (ParsedDoc, raw bytes or None)."""
    if (url and file) or not (url or file):
        raise _fail(400, "missing_pdf", "Send either a url or a file, not both.")

    if url and not need_bytes:
        hit = _url_cache.get(url)
        if hit and time.time() - hit[1] < URL_CACHE_TTL and hit[0] in _doc_cache:
            _doc_cache.move_to_end(hit[0])
            return _doc_cache[hit[0]], None

    data = await _fetch_pdf(url) if url else await _read_upload(file)
    _check_is_pdf(data)
    sha = hashlib.sha256(data).hexdigest()
    if url:
        _url_cache[url] = (sha, time.time())
        if len(_url_cache) > 200:
            oldest = sorted(_url_cache.items(), key=lambda kv: kv[1][1])[:50]
            for k, _ in oldest:
                _url_cache.pop(k, None)

    doc = _doc_cache.get(sha)
    if doc is None:
        async with _parse_slots:
            doc = await run_in_threadpool(_parse, data, sha)
        _cache_put(doc)
    else:
        _doc_cache.move_to_end(sha)
    return doc, data


# --------------------------------------------------------------------------
# Response builders
# --------------------------------------------------------------------------

def _render(blocks: list[Block], fmt: str) -> str:
    parts = []
    for b in blocks:
        if fmt == "markdown" and b.kind == "heading":
            parts.append(f"## {b.text}")
        else:
            parts.append(b.text)
    return "\n\n".join(parts)


def _paginate(blocks: list[Block], words_per_page: int) -> list[list[Block]]:
    pages, current, count = [], [], 0
    for b in blocks:
        words = len(b.text.split())
        if current and count + words > words_per_page:
            pages.append(current)
            current, count = [], 0
        current.append(b)
        count += words
    if current:
        pages.append(current)
    return pages


def _chapter_ranges(doc: ParsedDoc) -> list[tuple[Chapter, int, int]]:
    out = []
    for i, ch in enumerate(doc.chapters):
        end = doc.chapters[i + 1].block_start if i + 1 < len(doc.chapters) else len(doc.blocks)
        out.append((ch, ch.block_start, end))
    return out


def _read_response(doc: ParsedDoc, page: int, words_per_page: int, fmt: str) -> dict:
    pages = _paginate(doc.blocks, words_per_page)
    total = len(pages)
    if page > total:
        raise _fail(404, "page_out_of_range", f"This document has {total} page(s) at {words_per_page} words per page.")
    chunk = pages[page - 1]
    return {
        "title": doc.title,
        "author": doc.metadata.get("author"),
        "page": page,
        "total_pages": total,
        "words_per_page": words_per_page,
        "source_pages": [chunk[0].page, chunk[-1].page],
        "pdf_page_count": doc.page_count,
        "word_count": sum(len(b.text.split()) for b in doc.blocks),
        "chapters_detected": 0 if doc.chapter_source == "none" else len(doc.chapters),
        "cleaned": doc.stats,
        "format": fmt,
        "text": _render(chunk, fmt),
    }


def _chapters_response(doc: ParsedDoc, chapter: int | None, fmt: str) -> dict:
    ranges = _chapter_ranges(doc)
    listing = []
    for n, (ch, start, end) in enumerate(ranges, start=1):
        blocks = doc.blocks[start:end]
        listing.append({
            "number": n,
            "title": ch.title,
            "start_page": blocks[0].page if blocks else None,
            "end_page": blocks[-1].page if blocks else None,
            "word_count": sum(len(b.text.split()) for b in blocks),
        })
    body = {
        "title": doc.title,
        "author": doc.metadata.get("author"),
        "source": doc.chapter_source,
        "total_chapters": len(listing),
        "chapters": listing,
    }
    if chapter is not None:
        if chapter > len(ranges):
            raise _fail(404, "chapter_out_of_range", f"This document has {len(ranges)} chapter(s).")
        _, start, end = ranges[chapter - 1]
        body["chapter"] = {**listing[chapter - 1], "format": fmt, "text": _render(doc.blocks[start:end], fmt)}
    return body


def _parse_range(spec: str | None, page_count: int) -> tuple[int, int]:
    if spec is not None and spec.strip().lower() in ("", "{}", "null", "none", "all"):
        spec = None
    if not spec:
        return 1, min(page_count, MAX_TABLE_PAGES)
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec)
    if not m:
        raise _fail(400, "bad_pages", "table_pages should look like 3 or 2-10.")
    first, last = int(m.group(1)), int(m.group(2) or m.group(1))
    if first < 1 or last < first:
        raise _fail(400, "bad_pages", "table_pages should look like 3 or 2-10.")
    if first > page_count:
        raise _fail(404, "page_out_of_range", f"This PDF has {page_count} page(s).")
    last = min(last, page_count)
    if last - first + 1 > MAX_TABLE_PAGES:
        raise _fail(400, "bad_pages", f"Tables can be read from up to {MAX_TABLE_PAGES} pages per call.")
    return first, last


async def _info_response(url, file, tables: bool, table_pages: str | None) -> dict:
    doc, data = await _load(url, file, need_bytes=False)
    body = {
        **doc.metadata,
        "page_count": doc.page_count,
        "page_size": doc.page_size,
        "word_count": sum(len(b.text.split()) for b in doc.blocks),
        "has_outline": doc.chapter_source == "outline",
        "chapters_detected": 0 if doc.chapter_source == "none" else len(doc.chapters),
    }
    if tables:
        first, last = _parse_range(table_pages, doc.page_count)
        key = f"{first}-{last}"
        if key not in doc.tables:
            if data is None:
                doc, data = await _load(url, file, need_bytes=True)
            async with _parse_slots:
                doc.tables[key] = await run_in_threadpool(_parse_tables, data, first, last)
        found = doc.tables[key]
        body["table_pages"] = [first, last]
        body["table_count"] = len(found)
        body["tables"] = found
    return body


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

URL_HELP = "Link to a PDF (http or https, up to 25 MB and 400 pages)."
FMT = Query("text", pattern="^(text|markdown)$", description="text, or markdown with ## headings")


@router.get("/read", summary="Clean text, page by page (PDF by URL)")
async def read_get(
    url: str = Query(..., description=URL_HELP),
    page: int = Query(1, ge=1),
    words_per_page: int = Query(900, ge=100, le=5000),
    format: str = FMT,
):
    doc, _ = await _load(url, None)
    return _read_response(doc, page, words_per_page, format)


@router.post("/read", summary="Clean text, page by page (upload a PDF)")
async def read_post(
    file: UploadFile | None = File(None),
    url: str | None = Form(None, description=URL_HELP),
    page: int = Query(1, ge=1),
    words_per_page: int = Query(900, ge=100, le=5000),
    format: str = FMT,
):
    doc, _ = await _load(url, file)
    return _read_response(doc, page, words_per_page, format)


@router.get("/chapters", summary="Chapter list and chapter text (PDF by URL)")
async def chapters_get(
    url: str = Query(..., description=URL_HELP),
    chapter: int | None = Query(None, ge=1, description="Chapter number to return the text of"),
    format: str = FMT,
):
    doc, _ = await _load(url, None)
    return _chapters_response(doc, chapter, format)


@router.post("/chapters", summary="Chapter list and chapter text (upload a PDF)")
async def chapters_post(
    file: UploadFile | None = File(None),
    url: str | None = Form(None, description=URL_HELP),
    chapter: int | None = Query(None, ge=1),
    format: str = FMT,
):
    doc, _ = await _load(url, file)
    return _chapters_response(doc, chapter, format)


@router.get("/info", summary="Metadata and tables (PDF by URL)")
async def info_get(
    url: str = Query(..., description=URL_HELP),
    tables: bool = Query(True, description="Also extract tables"),
    table_pages: str | None = Query(None, description="Pages to scan for tables, e.g. 2-10 (max 50)"),
):
    return await _info_response(url, None, tables, table_pages)


@router.post("/info", summary="Metadata and tables (upload a PDF)")
async def info_post(
    file: UploadFile | None = File(None),
    url: str | None = Form(None, description=URL_HELP),
    tables: bool = Query(True),
    table_pages: str | None = Query(None),
):
    return await _info_response(url, file, tables, table_pages)
