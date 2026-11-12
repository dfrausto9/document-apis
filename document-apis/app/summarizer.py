"""Doc Summarizer API.

A FastAPI router for the document-apis service. It summarizes any text, web page
or PDF without an AI model: it picks the most important sentences straight from
the text (extractive summarizing), so results are fast, free to run and never
invent anything that isn't in the document.

    POST /summarize/text   summarize text you send as JSON
    GET  /summarize/url    summarize a web page or a PDF link
    POST /summarize/file   summarize an uploaded PDF or .txt / .md file

Every response has a short summary, key points, keywords, reading time and,
when the document has chapters or headings, a summary for each section.
"""

from __future__ import annotations

import html
import math
import re
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, File, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import pdf_reader
from .pdf_reader import _check_public_host, _fail

router = APIRouter(prefix="/summarize", tags=["Doc Summarizer"])

MAX_TEXT_CHARS = 500_000          # about 80,000 words
MAX_HTML_BYTES = 5 * 1024 * 1024  # 5 MB web pages
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024
MAX_SECTIONS = 60
WORDS_PER_MINUTE = 230
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 5
USER_AGENT = "DocSummarizer/1.0 (+https://rapidapi.com/RecipeScraperPro)"

LENGTHS = {"short": 3, "medium": 5, "long": 8}

STOPWORDS = set("""
a about above after again against all almost also although am among an and another any anyone
anything are aren't around as at back be became because become been before being below between
both but by can can't cannot could couldn't did didn't do does doesn't doing don't done down
during each either else enough even ever every few for from further get gets getting give given
go goes going gone got had hadn't has hasn't have haven't having he he'd he'll he's her here
here's hers herself him himself his how how's however i i'd i'll i'm i've if in into is isn't
it it's its itself just know last least less let let's like likely made make makes many may
maybe me might more most mostly much must mustn't my myself near need neither never new next no
nor not nothing now of off often on once one only onto or other others otherwise our ours
ourselves out over own per perhaps put quite rather really said same say says see seem seemed
seems several shall shan't she she'd she'll she's should shouldn't since so some something
sometimes still such than that that's the their theirs them themselves then there there's these
they they'd they'll they're they've thing things this those though through thus to too toward
towards two under until up upon us use used using very via was wasn't way we we'd we'll we're
we've well were weren't what what's whatever when when's where where's whether which while who
who's whole whom whose why why's will with within without won't would wouldn't yes yet you
you'd you'll you're you've your yours yourself yourselves
""".split())

_ABBREV = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "e.g", "i.e", "fig", "no",
    "vol", "inc", "ltd", "co", "corp", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
    "sept", "oct", "nov", "dec", "approx", "dept", "est", "u.s", "u.k", "al", "pp", "ch",
}
_WORD = re.compile(r"[A-Za-z][A-Za-z'’-]*[A-Za-z]|[A-Za-z]")
_SENT_END = re.compile(r"([.!?]+[\"'”’)\]]*)\s+(?=[\"'“‘(\[]?[A-Z0-9])")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_CHAPTER_LINE = re.compile(
    r"^\s*((chapter|part|book|section)\s+([0-9]+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve)\b.*|prologue|epilogue|introduction|preface|conclusion)\s*$",
    re.I,
)


# --------------------------------------------------------------------------
# Splitting text into sections, paragraphs and sentences
# --------------------------------------------------------------------------

def _clean_space(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _is_heading_line(line: str) -> str | None:
    """Return the heading text if this line looks like a heading, else None."""
    m = _MD_HEADING.match(line)
    if m:
        return m.group(1).strip()
    s = line.strip()
    if 0 < len(s) <= 80 and _CHAPTER_LINE.match(s):
        return s
    return None


def sections_from_text(text: str) -> list[dict]:
    """Split plain or markdown text into [{title, paragraphs}] using headings and
    "Chapter 3"-style lines. Text before the first heading becomes an untitled section."""
    sections: list[dict] = [{"title": None, "paragraphs": []}]
    para: list[str] = []

    def flush():
        if para:
            joined = " ".join(p.strip() for p in para).strip()
            if joined:
                sections[-1]["paragraphs"].append(joined)
            para.clear()

    for line in _clean_space(text).split("\n"):
        heading = _is_heading_line(line)
        if heading:
            flush()
            sections.append({"title": heading, "paragraphs": []})
        elif not line.strip():
            flush()
        else:
            para.append(line)
    flush()
    return [s for s in sections if s["paragraphs"]]


def split_sentences(paragraph: str) -> list[str]:
    paragraph = re.sub(r"\s+", " ", paragraph).strip()
    if not paragraph:
        return []
    parts, start = [], 0
    for m in _SENT_END.finditer(paragraph):
        candidate = paragraph[start:m.end(1)]
        last_word = re.findall(r"([A-Za-z][A-Za-z.]*)\.+[\"'”’)\]]*$", candidate)
        if last_word and (last_word[0].lower().rstrip(".") in _ABBREV or len(last_word[0]) == 1):
            continue  # "Dr. Smith", "J. R. R. Tolkien"
        parts.append(candidate.strip())
        start = m.end()
    tail = paragraph[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _words(text: str) -> list[str]:
    return [w.lower().replace("’", "'") for w in _WORD.findall(text)]


def _stem(word: str) -> str:
    """A light stemmer so 'reading', 'reads' and 'read' count together."""
    for suffix in ("ingly", "edly", "ing", "ies", "ied", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            if suffix in ("ies", "ied"):
                word += "y"
            break
    return word


def _content_terms(text: str) -> list[str]:
    return [_stem(w) for w in _words(text) if w not in STOPWORDS and len(w) > 2]


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

class _Sentence:
    __slots__ = ("text", "index", "section", "para_pos", "terms", "score")

    def __init__(self, text, index, section, para_pos):
        self.text = text
        self.index = index
        self.section = section
        self.para_pos = para_pos
        self.terms = set(_content_terms(text))
        self.score = 0.0


def _build_sentences(sections: list[dict]) -> list[_Sentence]:
    out: list[_Sentence] = []
    for s_i, sec in enumerate(sections):
        for para in sec["paragraphs"]:
            for p_i, sent in enumerate(split_sentences(para)):
                out.append(_Sentence(sent, len(out), s_i, p_i))
    return out


def _score(sentences: list[_Sentence], sections: list[dict]) -> Counter:
    """Score each sentence by how many of the document's important words it holds,
    with a small boost for opening sentences and for words used in headings."""
    freq: Counter = Counter()
    for s in sentences:
        freq.update(_content_terms(s.text))
    if not freq:
        return freq
    top = max(freq.values())
    weight = {t: c / top for t, c in freq.items()}
    title_terms = set()
    for sec in sections:
        if sec["title"]:
            title_terms.update(_content_terms(sec["title"]))

    total = len(sentences)
    for s in sentences:
        n_words = len(s.text.split())
        if not s.terms or n_words < 5:
            s.score = 0.0
            continue
        base = sum(weight.get(t, 0) for t in s.terms) / math.sqrt(len(s.terms))
        if s.para_pos == 0:
            base *= 1.15                      # topic sentences
        if s.index < max(3, total * 0.05):
            base *= 1.1                       # the opening usually frames the document
        if title_terms & s.terms:
            base *= 1.1
        if n_words > 60:
            base *= 0.75                      # very long sentences make poor summaries
        if s.text.endswith("?"):
            base *= 0.8
        base *= _quality(s.text)
        s.score = base
    return freq


_SECTION_NUM = re.compile(r"^\s*(\d+(\.\d+)+|[IVX]+\.)\s")
_END_OK = re.compile(r"[.!?][\"'”’)\]]*$")


def _quality(text: str) -> float:
    """Push down things that aren't real prose: table rows, figure labels, numbered
    headings run into a sentence, and fragments with no ending punctuation."""
    tokens = text.split()
    numeric = sum(1 for t in tokens if any(ch.isdigit() for ch in t))
    letters = sum(ch.isalpha() for ch in text)
    factor = 1.0
    if tokens and numeric / len(tokens) > 0.25:
        factor *= 0.2
    if letters < 0.6 * len(text.replace(" ", "")):
        factor *= 0.3
    if not _END_OK.search(text):
        factor *= 0.35
    if _SECTION_NUM.match(text):
        factor *= 0.3
    return factor


def _pick(sentences: list[_Sentence], n: int) -> list[_Sentence]:
    """Take the best sentences, skipping ones that repeat what's already picked."""
    chosen: list[_Sentence] = []
    for s in sorted(sentences, key=lambda x: x.score, reverse=True):
        if len(chosen) >= n or s.score <= 0:
            break
        if any(_overlap(s.terms, c.terms) > 0.55 for c in chosen):
            continue
        chosen.append(s)
    return chosen


def _overlap(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _keywords(sentences: list[_Sentence], limit: int) -> list[str]:
    """Most telling words and two-word phrases, shown in their natural spelling."""
    singles: Counter = Counter()
    pairs: Counter = Counter()
    spelling: dict[str, Counter] = {}
    for s in sentences:
        words = _words(s.text)
        for w in words:
            if w not in STOPWORDS and len(w) > 2:
                st = _stem(w)
                singles[st] += 1
                spelling.setdefault(st, Counter())[w] += 1
        for a, b in zip(words, words[1:]):
            if a not in STOPWORDS and b not in STOPWORDS and len(a) > 2 and len(b) > 2:
                pairs[f"{a} {b}"] += 1

    out: list[str] = []
    used: set[str] = set()
    for phrase, c in pairs.most_common(limit * 2):
        if c < 2 or len(out) >= max(1, limit // 3):
            break
        out.append(phrase)
        used.update(_stem(w) for w in phrase.split())
    for st, c in singles.most_common(limit * 3):
        if len(out) >= limit:
            break
        if st in used or c < 2:
            continue
        out.append(spelling[st].most_common(1)[0][0])
        used.add(st)
    return out


# --------------------------------------------------------------------------
# Building the response
# --------------------------------------------------------------------------

def summarize_sections(
    sections: list[dict],
    *,
    title: str | None = None,
    sentences: int = 5,
    key_points: int = 5,
    keywords: int = 10,
    by_section: bool = True,
    section_sentences: int = 2,
) -> dict:
    if not sections:
        raise _fail(422, "no_text", "There's no readable text to summarize.")
    sents = _build_sentences(sections)
    word_count = sum(len(p.split()) for sec in sections for p in sec["paragraphs"])
    if word_count < 40 or len(sents) < 3:
        raise _fail(422, "too_short", "The text is too short to summarize. Send at least a few sentences.")

    _score(sents, sections)
    summary = sorted(_pick(sents, sentences), key=lambda s: s.index)
    summary_text = " ".join(s.text for s in summary)
    summary_ids = {s.index for s in summary}

    # Key points: spread across the document, so they add to the summary instead of repeating it.
    pool = [s for s in sents if s.index not in summary_ids]
    picked = _pick(pool, key_points)
    if len(picked) < key_points:
        extra = [s for s in summary if all(_overlap(s.terms, p.terms) <= 0.55 for p in picked)]
        picked += extra[: key_points - len(picked)]
    points = [s.text for s in sorted(picked, key=lambda s: s.index)]

    body = {
        "title": title or next((s["title"] for s in sections if s["title"]), None),
        "word_count": word_count,
        "reading_time_minutes": max(1, round(word_count / WORDS_PER_MINUTE)),
        "summary": summary_text,
        "summary_word_count": len(summary_text.split()),
        "key_points": points,
        "keywords": _keywords(sents, keywords),
        "sections_found": sum(1 for s in sections if s["title"]),
    }

    if by_section:
        out = []
        titled = [(i, s) for i, s in enumerate(sections) if s["title"]]
        for i, sec in titled[:MAX_SECTIONS]:
            mine = [s for s in sents if s.section == i]
            words = sum(len(p.split()) for p in sec["paragraphs"])
            best = sorted(_pick(mine, section_sentences), key=lambda s: s.index)
            out.append({
                "number": len(out) + 1,
                "title": sec["title"],
                "word_count": words,
                "summary": " ".join(s.text for s in best) if best else " ".join(sec["paragraphs"])[:400],
            })
        body["sections"] = out
    return body


def _options(length: str | None, sentences) -> int:
    """sentences wins over length. The RapidAPI playground sends '{}' for empty
    optional numbers, so blank-looking values are treated as not set."""
    if isinstance(sentences, str):
        raw = sentences.strip().lower()
        if raw in ("", "{}", "null", "none", "undefined"):
            sentences = None
        elif raw.isdigit() and 1 <= int(raw) <= 30:
            sentences = int(raw)
        else:
            raise _fail(400, "bad_sentences", "sentences should be a number from 1 to 30.")
    if sentences:
        return sentences
    return LENGTHS[length or "medium"]


# --------------------------------------------------------------------------
# Web pages
# --------------------------------------------------------------------------

class _PageText(HTMLParser):
    """Pull the readable text out of a web page: headings, paragraphs and list items,
    skipping menus, scripts, footers and other page furniture."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg",
            "button", "select", "iframe", "template", "figure"}
    BLOCKS = {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "td", "dd"}
    HEADINGS = {"h1", "h2", "h3"}
    JUNK = re.compile(r"(comment|share|social|related|newsletter|subscribe|cookie|promo|advert|"
                      r"sidebar|menu|breadcrumb|footer|popup|modal)", re.I)

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.stack: list[tuple[str, bool]] = []
        self.current: list[str] | None = None
        self.current_tag = ""
        self.items: list[tuple[str, str]] = []
        self.title = ""
        self.in_title = False
        self.in_article = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        junk = bool(self.JUNK.search(f"{a.get('class', '')} {a.get('id', '')}")) and tag in {"div", "section", "ul", "aside"}
        skip = tag in self.SKIP or junk or a.get("aria-hidden") == "true" or "hidden" in a
        if tag in {"br", "img", "hr", "meta", "link", "input"}:
            return
        self.stack.append((tag, skip))
        if skip:
            self.skip_depth += 1
        if tag in {"article", "main"}:
            self.in_article += 1
        if tag == "title":
            self.in_title = True
        if not self.skip_depth and tag in self.BLOCKS:
            self._close()
            self.current, self.current_tag = [], tag

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.BLOCKS:
            self._close()
        while self.stack:
            t, skip = self.stack.pop()
            if skip:
                self.skip_depth -= 1
            if t in {"article", "main"}:
                self.in_article -= 1
            if t == tag:
                break

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.skip_depth or self.current is None:
            return
        self.current.append(data)

    def _close(self):
        if self.current is not None:
            text = re.sub(r"\s+", " ", "".join(self.current)).strip()
            if text:
                self.items.append((self.current_tag, text))
        self.current = None


def sections_from_html(raw: str) -> tuple[str | None, list[dict]]:
    parser = _PageText()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    parser._close()
    title = re.sub(r"\s+", " ", html.unescape(parser.title)).strip() or None
    sections: list[dict] = [{"title": None, "paragraphs": []}]
    for tag, text in parser.items:
        if tag in _PageText.HEADINGS:
            if tag == "h1" and not sections[-1]["paragraphs"] and sections[-1]["title"] is None:
                title = title or text
                continue
            sections.append({"title": text, "paragraphs": []})
        elif tag == "li" and len(text.split()) < 6:
            continue  # menu-style list items
        elif len(text.split()) >= 4 or text.endswith((".", "!", "?")):
            sections[-1]["paragraphs"].append(text)
    return title, [s for s in sections if s["paragraphs"]]


async def _fetch(url: str) -> tuple[bytes, str]:
    """Download a link safely (no private addresses, every redirect checked)."""
    current = url
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5"},
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
                    ctype = resp.headers.get("content-type", "").lower()
                    limit = pdf_reader.MAX_BYTES if "pdf" in ctype else MAX_HTML_BYTES
                    chunks, size = [], 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise _fail(413, "too_large", "That link is too large to summarize.")
                        chunks.append(chunk)
                    return b"".join(chunks), ctype
            except httpx.TimeoutException:
                raise _fail(504, "fetch_timeout", "The link took too long to respond.")
            except httpx.HTTPError:
                raise _fail(502, "fetch_failed", "Could not download that link.")
    raise _fail(502, "fetch_failed", "The link redirected too many times.")


def _decode(data: bytes, ctype: str = "") -> str:
    m = re.search(r"charset=([\w-]+)", ctype)
    for enc in ([m.group(1)] if m else []) + ["utf-8", "cp1252", "latin-1"]:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "ignore")


# --------------------------------------------------------------------------
# PDFs (reuses the Clean PDF Reader parser)
# --------------------------------------------------------------------------

async def _sections_from_pdf_bytes(data: bytes) -> tuple[str | None, list[dict], dict]:
    import hashlib

    pdf_reader._check_is_pdf(data)
    sha = hashlib.sha256(data).hexdigest()
    doc = pdf_reader._doc_cache.get(sha)
    if doc is None:
        async with pdf_reader._parse_slots:
            doc = await run_in_threadpool(pdf_reader._parse, data, sha)
        pdf_reader._cache_put(doc)
    return doc.title, _sections_from_doc(doc), {"type": "pdf", "pdf_pages": doc.page_count,
                                                  "chapter_source": doc.chapter_source}


def _sections_from_doc(doc) -> list[dict]:
    sections: list[dict] = []
    if doc.chapter_source != "none" and doc.chapters:
        first = doc.chapters[0].block_start
        if first > 0:
            sections.append({"title": None, "paragraphs": [b.text for b in doc.blocks[:first] if b.kind == "para"]})
        for ch, start, end in pdf_reader._chapter_ranges(doc):
            sections.append({"title": ch.title,
                             "paragraphs": [b.text for b in doc.blocks[start:end] if b.kind == "para"]})
    else:
        sections.append({"title": None, "paragraphs": []})
        for b in doc.blocks:
            if b.kind == "heading":
                sections.append({"title": b.text, "paragraphs": []})
            else:
                sections[-1]["paragraphs"].append(b.text)
    return [s for s in sections if s["paragraphs"]]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

LENGTH = Query("medium", pattern="^(short|medium|long)$",
               description="short (3 sentences), medium (5) or long (8)")
SENTENCES = Query(None, description="Exact number of summary sentences, 1 to 30 (overrides length)")
KEY_POINTS = Query(5, ge=0, le=15, description="How many key points to return")
KEYWORDS = Query(10, ge=0, le=30, description="How many keywords to return")
BY_SECTION = Query(True, description="Also summarize each chapter or section")


class TextIn(BaseModel):
    text: str = Field(..., description="The text to summarize. Markdown headings and 'Chapter 1' lines become sections.")
    title: str | None = Field(None, description="Optional title")
    length: str = Field("medium", pattern="^(short|medium|long)$")
    sentences: int | None = Field(None, ge=1, le=30)
    key_points: int = Field(5, ge=0, le=15)
    keywords: int = Field(10, ge=0, le=30)
    by_section: bool = True


@router.post("/text", summary="Summarize text you send")
async def summarize_text(body: TextIn):
    text = body.text or ""
    if len(text) > MAX_TEXT_CHARS:
        raise _fail(413, "too_large", f"Text can be up to {MAX_TEXT_CHARS:,} characters.")
    result = await run_in_threadpool(
        summarize_sections, sections_from_text(text), title=body.title,
        sentences=_options(body.length, body.sentences), key_points=body.key_points,
        keywords=body.keywords, by_section=body.by_section,
    )
    return {"source": {"type": "text"}, **result}


@router.get("/url", summary="Summarize a web page or PDF link")
async def summarize_url(
    url: str = Query(..., description="Link to a web page (article, blog post, docs) or a PDF"),
    length: str = LENGTH,
    sentences: str | None = SENTENCES,
    key_points: int = KEY_POINTS,
    keywords: int = KEYWORDS,
    by_section: bool = BY_SECTION,
):
    data, ctype = await _fetch(url)
    if "pdf" in ctype or data[:5] == b"%PDF-":
        title, sections, source = await _sections_from_pdf_bytes(data)
    elif "html" in ctype or b"<html" in data[:2000].lower() or b"<!doctype" in data[:200].lower():
        title, sections = await run_in_threadpool(sections_from_html, _decode(data, ctype))
        source = {"type": "web_page"}
    elif ctype.startswith("text/") or not ctype:
        title, sections = None, sections_from_text(_decode(data, ctype)[:MAX_TEXT_CHARS])
        source = {"type": "text"}
    else:
        raise _fail(415, "unsupported_type", "That link isn't a web page, PDF or text file.")
    result = await run_in_threadpool(
        summarize_sections, sections, title=title, sentences=_options(length, sentences),
        key_points=key_points, keywords=keywords, by_section=by_section,
    )
    return {"source": {**source, "url": url}, **result}


@router.post("/file", summary="Summarize an uploaded PDF or text file")
async def summarize_file(
    file: UploadFile = File(..., description="A PDF, .txt or .md file"),
    length: str = LENGTH,
    sentences: str | None = SENTENCES,
    key_points: int = KEY_POINTS,
    keywords: int = KEYWORDS,
    by_section: bool = BY_SECTION,
):
    data = await pdf_reader._read_upload(file)
    if not data:
        raise _fail(400, "empty_file", "The file is empty.")
    name = (file.filename or "").lower()
    if data[:1024].find(b"%PDF") != -1 or name.endswith(".pdf"):
        title, sections, source = await _sections_from_pdf_bytes(data)
    else:
        if len(data) > MAX_TEXT_FILE_BYTES:
            raise _fail(413, "too_large", "Text files can be up to 5 MB.")
        if b"\x00" in data[:4096]:
            raise _fail(415, "unsupported_type", "Upload a PDF, .txt or .md file.")
        title, sections, source = None, sections_from_text(_decode(data)[:MAX_TEXT_CHARS]), {"type": "text"}
    result = await run_in_threadpool(
        summarize_sections, sections, title=title, sentences=_options(length, sentences),
        key_points=key_points, keywords=keywords, by_section=by_section,
    )
    return {"source": {**source, "filename": file.filename}, **result}
