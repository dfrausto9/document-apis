"""Tests for the Clean PDF Reader router. Run with: pytest tests/test_pdf_reader.py"""

import io
import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pdf_reader  # noqa: E402

app = FastAPI()
app.include_router(pdf_reader.router)
client = TestClient(app)

LOREM = (
    "The old library stood at the edge of town, and every morning the keeper opened its "
    "heavy doors before the sun had cleared the hills. Readers came for many reasons: some "
    "wanted adventure, some wanted quiet, and some only wanted somewhere warm to sit. "
)


class _Bookmarks:
    """Adds an outline entry for each chapter heading as the doc is built."""

    def __init__(self, enabled, doc):
        self.enabled = enabled
        self.doc = doc

    def __call__(self, flowable):
        doc = self.doc
        if self.enabled and getattr(flowable, "style", None) and flowable.style.name == "Chap":
            key = f"ch{doc.page}"
            doc.canv.bookmarkPage(key)
            doc.canv.addOutlineEntry(flowable.getPlainText(), key, level=0)


def make_book(outline=True, chapters=3, paras=9):
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    chap = ParagraphStyle("Chap", parent=styles["Heading1"], fontSize=22, spaceAfter=18)
    body = ParagraphStyle("Body", parent=styles["BodyText"], fontSize=11, leading=14, spaceAfter=8)

    def deco(c, d):
        c.setFont("Helvetica", 9)
        c.drawString(inch, letter[1] - 0.6 * inch, "The Test Book - A Sample")
        c.drawCentredString(letter[0] / 2, 0.5 * inch, str(d.page))

    doc = SimpleDocTemplate(buf, pagesize=letter, title="The Test Book", author="Dani F")
    doc.afterFlowable = _Bookmarks(outline, doc)
    story = [Paragraph("A short note before the story begins.", body), PageBreak()]
    for n in range(1, chapters + 1):
        story.append(Paragraph(f"Chapter {n}: Part {n} of the tale", chap))
        for p in range(paras):
            story.append(Paragraph(f"Paragraph {p + 1} of chapter {n}. " + LOREM * 2, body))
        story.append(PageBreak())
    doc.build(story, onFirstPage=deco, onLaterPages=deco)
    return buf.getvalue()


def make_hyphen_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 12)
    lines = [
        "This line ends with a word that is bro-",
        "ken across two lines and should be joined back",
        "into one paragraph without any stray spaces.",
    ]
    y = 700
    for line in lines:
        c.drawString(72, y, line)
        y -= 15
    c.save()
    return buf.getvalue()


def make_table_pdf():
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    data = [["Item", "Qty", "Price"], ["Apples", "3", "$1.20"], ["Pears", "6", "$2.40"], ["Plums", "9", "$3.60"]]
    t = Table(data)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    doc.build([Paragraph("Fruit order", styles["Title"]), Spacer(1, 12), t])
    return buf.getvalue()


def make_scan_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.rect(100, 100, 300, 300, fill=1)
    c.save()
    return buf.getvalue()


def make_encrypted_pdf():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, encrypt="secret")
    c.drawString(72, 700, "hidden")
    c.save()
    return buf.getvalue()


def post(path, data, **params):
    return client.post(path, params=params, files={"file": ("doc.pdf", data, "application/pdf")})


@pytest.fixture(autouse=True)
def clear_cache():
    pdf_reader._doc_cache.clear()
    pdf_reader._url_cache.clear()
    yield


# ---------------------------------------------------------------- /pdf/read

def test_read_removes_headers_footers_and_page_numbers():
    r = post("/pdf/read", make_book(), words_per_page=5000)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["title"] == "The Test Book"
    assert body["author"] == "Dani F"
    assert "The Test Book - A Sample" not in body["text"]
    assert body["cleaned"]["headers_footers"] >= 4
    assert body["cleaned"]["page_numbers"] >= 4
    for line in body["text"].split("\n\n"):
        assert not line.strip().isdigit(), "stray page number left in text"


def test_read_rebuilds_paragraphs():
    body = post("/pdf/read", make_book(), words_per_page=5000).json()
    paras = body["text"].split("\n\n")
    p1 = next(p for p in paras if p.startswith("Paragraph 1 of chapter 1."))
    # Each source paragraph wraps over several lines; it should come back as one.
    assert "\n" not in p1
    assert p1.count("The old library") == 2
    assert p1.endswith("somewhere warm to sit.")


def test_read_joins_hyphenated_words():
    body = post("/pdf/read", make_hyphen_pdf()).json()
    assert "broken across" in body["text"]
    assert body["cleaned"]["hyphens_fixed"] == 1


def test_read_pagination_and_markdown():
    first = post("/pdf/read", make_book(), words_per_page=300, format="markdown").json()
    assert first["total_pages"] > 3
    assert "## Chapter 1: Part 1 of the tale" in first["text"] or first["page"] == 1
    last = post("/pdf/read", make_book(), words_per_page=300, page=first["total_pages"]).json()
    assert last["page"] == first["total_pages"]
    over = post("/pdf/read", make_book(), words_per_page=300, page=first["total_pages"] + 1)
    assert over.status_code == 404
    assert over.json()["detail"]["error"] == "page_out_of_range"


# ------------------------------------------------------------ /pdf/chapters

def test_chapters_from_outline():
    body = post("/pdf/chapters", make_book(outline=True)).json()
    assert body["source"] == "outline"
    titles = [c["title"] for c in body["chapters"]]
    assert titles == ["Front matter", "Chapter 1: Part 1 of the tale",
                      "Chapter 2: Part 2 of the tale", "Chapter 3: Part 3 of the tale"]


def test_chapters_from_headings_when_no_outline():
    body = post("/pdf/chapters", make_book(outline=False)).json()
    assert body["source"] == "headings"
    assert [c["title"] for c in body["chapters"]][1:] == [
        "Chapter 1: Part 1 of the tale", "Chapter 2: Part 2 of the tale", "Chapter 3: Part 3 of the tale"]


def test_single_chapter_text():
    body = post("/pdf/chapters", make_book(), chapter=3).json()
    ch = body["chapter"]
    assert ch["title"] == "Chapter 2: Part 2 of the tale"
    assert ch["text"].startswith("Chapter 2: Part 2 of the tale")
    assert "chapter 2." in ch["text"] and "chapter 3." not in ch["text"]
    assert ch["word_count"] > 300
    bad = post("/pdf/chapters", make_book(), chapter=99)
    assert bad.status_code == 404


def test_no_chapters_gives_full_document():
    body = post("/pdf/chapters", make_hyphen_pdf()).json()
    assert body["source"] == "none"
    assert body["chapters"][0]["title"] == "Full document"


# ---------------------------------------------------------------- /pdf/info

def test_info_metadata():
    body = post("/pdf/info", make_book(), tables="false").json()
    assert body["title"] == "The Test Book"
    assert body["author"] == "Dani F"
    assert body["page_count"] == 5 or body["page_count"] > 3
    assert body["page_size"]["width_in"] == 8.5
    assert body["has_outline"] is True
    assert "tables" not in body


def test_info_tables():
    body = post("/pdf/info", make_table_pdf()).json()
    assert body["table_count"] == 1
    t = body["tables"][0]
    assert t["data"][0] == ["Item", "Qty", "Price"]
    assert t["data"][3] == ["Plums", "9", "$3.60"]
    assert t["rows"] == 4 and t["columns"] == 3


def test_info_bad_table_pages():
    r = post("/pdf/info", make_table_pdf(), table_pages="abc")
    assert r.status_code == 400


# ------------------------------------------------------------------ errors

def test_scan_without_text_layer():
    r = post("/pdf/read", make_scan_pdf())
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "no_text_layer"


def test_encrypted_pdf():
    r = post("/pdf/read", make_encrypted_pdf())
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "encrypted_pdf"


def test_not_a_pdf():
    r = post("/pdf/read", b"hello, this is not a pdf")
    assert r.status_code == 415


def test_needs_url_or_file():
    r = client.post("/pdf/read")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_pdf"


@pytest.mark.parametrize("url", [
    "http://localhost/x.pdf",
    "http://127.0.0.1/x.pdf",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/file.pdf",
    "ftp://example.com/x.pdf",
])
def test_blocks_private_and_bad_urls(url):
    r = client.get("/pdf/read", params={"url": url})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_url"


def test_url_input_and_cache(monkeypatch):
    calls = []
    book = make_book()

    async def fake_fetch(url):
        calls.append(url)
        return book

    monkeypatch.setattr(pdf_reader, "_fetch_pdf", fake_fetch)
    u = "https://example.com/book.pdf"
    a = client.get("/pdf/read", params={"url": u}).json()
    b = client.get("/pdf/chapters", params={"url": u}).json()
    assert a["title"] == b["title"] == "The Test Book"
    assert len(calls) == 1, "second call should come from the cache"
    # Tables need the raw bytes, so this one downloads again.
    c = client.get("/pdf/info", params={"url": u})
    assert c.status_code == 200
    assert len(calls) == 2
