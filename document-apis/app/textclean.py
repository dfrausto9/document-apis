"""Turn a raw Project Gutenberg .txt file into clean, readable text.

- removes the Project Gutenberg header and license footer
- re-joins hard-wrapped lines into real paragraphs
- finds chapter headings
- splits the text into pages
"""
import re

_START = re.compile(r"^\*{3}\s*START OF (THE|THIS) PROJECT GUTENBERG EBOOK.*?\*{3}\s*$", re.I | re.M)
_END = re.compile(r"^\*{3}\s*END OF (THE|THIS) PROJECT GUTENBERG EBOOK.*$", re.I | re.M)
_OLD_END = re.compile(r"^End of (the )?Project Gutenberg'?s? .*$", re.I | re.M)

_HEADING = re.compile(
    r"^(?:"
    r"(?:CHAPTER|Chapter|BOOK|Book|PART|Part|CANTO|Canto|SECTION|Section|LETTER|Letter)\s+"
    r"(?:[IVXLCDM]+|\d+|[A-Z][a-z]+|[A-Z]+)\b.*"
    r"|(?:[IVXLCDM]{1,7})\.?(?:\s+.*)?"
    r"|PREFACE|Preface|INTRODUCTION|Introduction|PROLOGUE|Prologue|EPILOGUE|Epilogue"
    r")$"
)


def strip_gutenberg_boilerplate(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    m = _START.search(text)
    if m:
        text = text[m.end():]
    m = _END.search(text) or _OLD_END.search(text)
    if m:
        text = text[: m.start()]
    return text.strip("\n ")


def _is_heading(block: str) -> bool:
    line = block.strip()
    if "\n" in line or not line or len(line) > 80:
        return False
    return bool(_HEADING.match(line))


def to_paragraphs(text: str) -> list[str]:
    """Split on blank lines and un-wrap the hard line breaks inside each paragraph.
    Short indented lines (poetry, verse, lists) keep their line breaks."""
    blocks = re.split(r"\n\s*\n", text)
    paras: list[str] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        verse = len(lines) > 1 and all(len(ln) < 60 for ln in lines) and any(ln.startswith(" ") for ln in lines)
        if verse:
            paras.append("\n".join(ln.strip() for ln in lines))
        else:
            joined = " ".join(ln.strip() for ln in lines)
            paras.append(re.sub(r"\s{2,}", " ", joined).strip())
    return [p for p in paras if p]


_CREDIT = re.compile(r"^(produced by|e-?text prepared by|transcribed from|this e-?book was produced)\b", re.I)


def drop_credits(paras: list[str]) -> list[str]:
    """Remove the volunteer 'Produced by …' credit lines at the very start."""
    i = 0
    while i < min(len(paras), 4) and _CREDIT.match(paras[i]):
        i += 1
    return paras[i:]


def find_chapters(paras: list[str]) -> list[dict]:
    chapters = []
    for i, p in enumerate(paras):
        if _is_heading(p):
            chapters.append({"title": p.strip(), "paragraph_index": i})
    return chapters


def paginate(paras: list[str], words_per_page: int = 900) -> list[list[str]]:
    pages: list[list[str]] = []
    current: list[str] = []
    count = 0
    for p in paras:
        w = len(p.split())
        if current and count + w > words_per_page:
            pages.append(current)
            current, count = [], 0
        current.append(p)
        count += w
    if current:
        pages.append(current)
    return pages or [[]]


def clean_book(raw: str, words_per_page: int = 900) -> dict:
    body = strip_gutenberg_boilerplate(raw)
    paras = drop_credits(to_paragraphs(body))
    pages = paginate(paras, words_per_page)
    # map each chapter to the page it starts on
    starts, idx = [], 0
    for n, page in enumerate(pages, start=1):
        starts.append((idx, n))
        idx += len(page)
    chapters = []
    for ch in find_chapters(paras):
        page_no = 1
        for first_idx, n in starts:
            if first_idx <= ch["paragraph_index"]:
                page_no = n
        chapters.append({"title": ch["title"], "page": page_no})
    return {
        "paragraphs": paras,
        "pages": pages,
        "chapters": chapters,
        "word_count": sum(len(p.split()) for p in paras),
    }
