"""Tests for the Doc Summarizer router. Run with: pytest tests/test_summarizer.py"""

import io
import os
import sys

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import summarizer  # noqa: E402

app = FastAPI()
app.include_router(summarizer.router)
client = TestClient(app)

ARTICLE = """# Keeping Honey Bees in a Small Garden

Honey bees can thrive in a small garden when the hive is placed with care. A sunny spot that is
sheltered from strong wind keeps the colony warm and busy. Most beginners start with a single hive
and a mentor who has kept bees for several years.

## Choosing a hive

The Langstroth hive is the most common choice because its frames are easy to inspect. Top bar
hives cost less to build, but they hold less honey. Whatever you pick, the hive should face away
from paths so the bees' flight line does not cross where people walk. Dr. Smith, a local keeper,
says the entrance should face south-east to catch the morning sun.

## Feeding and flowers

Bees need a steady supply of nectar and pollen from spring to autumn. Planting lavender, borage
and clover gives the colony food close to home. A shallow dish of water with stones lets bees drink
without drowning. In early spring, a little sugar syrup helps a weak colony build up.

## Harvesting honey

Honey is ready when most of the comb is capped with wax. Take only the surplus, and leave the bees
enough honey to survive the winter. A small garden hive can give twenty to thirty pounds of honey
in a good year. Extracting the honey is sticky work, so most keepers do it in a garage or shed.
"""


def test_text_summary_has_all_parts():
    r = client.post("/summarize/text", json={"text": ARTICLE, "length": "short", "key_points": 3})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["title"] == "Keeping Honey Bees in a Small Garden"
    assert len(summarizer.split_sentences(j["summary"])) == 3
    assert 1 <= len(j["key_points"]) <= 3
    assert not set(j["key_points"]) & set(summarizer.split_sentences(j["summary"]))
    assert "honey" in " ".join(j["keywords"]).lower()
    assert [s["title"] for s in j["sections"]] == [
        "Keeping Honey Bees in a Small Garden", "Choosing a hive", "Feeding and flowers", "Harvesting honey"]
    assert j["reading_time_minutes"] >= 1
    assert j["summary_word_count"] < j["word_count"]


def test_summary_sentences_keep_document_order():
    j = client.post("/summarize/text", json={"text": ARTICLE, "sentences": 4}).json()
    sents = summarizer.split_sentences(j["summary"])
    flat = " ".join(ARTICLE.split())
    positions = [flat.find(" ".join(s.split())[:40]) for s in sents]
    assert all(p >= 0 for p in positions)
    assert positions == sorted(positions)


def test_sentence_splitter_keeps_abbreviations():
    s = summarizer.split_sentences("Dr. Smith met J. R. Tolkien at 3 p.m. on Main St. today. Then they left.")
    assert s == ["Dr. Smith met J. R. Tolkien at 3 p.m. on Main St. today.", "Then they left."]


def test_chapter_lines_become_sections():
    text = "\n\n".join(
        f"Chapter {n}\n\n" + " ".join(f"In chapter {n} the traveller walked to village {n} and met a {w} farmer."
                                      for w in ("kind", "tired", "cheerful", "quiet"))
        for n in (1, 2, 3)
    )
    j = client.post("/summarize/text", json={"text": text}).json()
    assert [s["title"] for s in j["sections"]] == ["Chapter 1", "Chapter 2", "Chapter 3"]


def test_by_section_false_skips_sections():
    j = client.post("/summarize/text", json={"text": ARTICLE, "by_section": False}).json()
    assert "sections" not in j


def test_too_short_text():
    r = client.post("/summarize/text", json={"text": "Bees make honey. They live in hives."})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "too_short"


def test_bad_length():
    r = client.post("/summarize/text", json={"text": ARTICLE, "length": "huge"})
    assert r.status_code == 422


HTML = f"""<!doctype html><html><head><title>Garden Bees | Example Blog</title>
<script>var tracking = "do not read me";</script></head><body>
<nav><ul><li>Home</li><li>About</li><li>Shop</li></ul></nav>
<div class="cookie-banner"><p>We use cookies to improve your experience on this site, okay?</p></div>
<article><h1>Keeping Honey Bees in a Small Garden</h1>
{''.join(f'<p>{p}</p>' if not p.startswith('#') else f'<h2>{p.strip("# ")}</h2>'
         for p in ARTICLE.split(chr(10) + chr(10))[1:])}
</article>
<footer><p>Copyright 2026 Example Blog. All rights reserved worldwide forever.</p></footer>
</body></html>"""


def test_html_extraction_skips_page_furniture():
    title, sections = summarizer.sections_from_html(HTML)
    text = " ".join(p for s in sections for p in s["paragraphs"])
    assert title == "Garden Bees | Example Blog"
    assert "cookies" not in text and "tracking" not in text and "Copyright" not in text
    assert [s["title"] for s in sections] == ["Choosing a hive", "Feeding and flowers", "Harvesting honey"] or \
        [s["title"] for s in sections][1:] == ["Choosing a hive", "Feeding and flowers", "Harvesting honey"]


@respx.mock
def test_url_web_page(monkeypatch):
    monkeypatch.setattr(summarizer, "_check_public_host", lambda url: None)
    respx.get("https://example.com/bees").mock(
        return_value=httpx.Response(200, text=HTML, headers={"content-type": "text/html; charset=utf-8"}))
    r = client.get("/summarize/url", params={"url": "https://example.com/bees", "length": "short"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["source"]["type"] == "web_page"
    assert j["sections_found"] == 3
    assert "cookies" not in j["summary"]


def test_url_refuses_private_address():
    r = client.get("/summarize/url", params={"url": "http://127.0.0.1/admin"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_url"


def _pdf_bytes() -> bytes:
    buf = io.BytesIO()
    styles = getSampleStyleSheet()
    chap = ParagraphStyle("Chap", parent=styles["Heading1"], fontSize=20)
    story = []
    for n, topic in enumerate(["the river", "the mountain", "the city"], start=1):
        story.append(Paragraph(f"Chapter {n}", chap))
        story.append(Spacer(1, 12))
        for k in range(6):
            story.append(Paragraph(
                f"The explorers studied {topic} for many days and wrote careful notes about {topic}. "
                f"On day {k + 1} they mapped another stretch of {topic} and shared the results with the "
                f"village elders, who knew {topic} better than anyone.", styles["BodyText"]))
        story.append(PageBreak())
    SimpleDocTemplate(buf, pagesize=letter).build(story)
    return buf.getvalue()


def test_file_pdf_upload():
    r = client.post("/summarize/file", files={"file": ("notes.pdf", _pdf_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["source"]["type"] == "pdf"
    assert j["source"]["filename"] == "notes.pdf"
    assert len(j["sections"]) == 3
    assert "river" in j["sections"][0]["summary"]


def test_file_text_upload():
    r = client.post("/summarize/file", files={"file": ("bees.md", ARTICLE.encode(), "text/markdown")},
                    params={"length": "long"})
    assert r.status_code == 200, r.text
    assert r.json()["source"]["type"] == "text"


def test_file_rejects_binary():
    r = client.post("/summarize/file", files={"file": ("x.bin", b"\x00\x01\x02" * 100, "application/octet-stream")})
    assert r.status_code == 415


def test_playground_blank_sentences_is_ignored(monkeypatch):
    r = client.post("/summarize/file", files={"file": ("bees.md", ARTICLE.encode(), "text/markdown")},
                    params={"sentences": "{}", "length": "short"})
    assert r.status_code == 200, r.text
    assert len(summarizer.split_sentences(r.json()["summary"])) == 3
    r = client.post("/summarize/file", files={"file": ("bees.md", ARTICLE.encode(), "text/markdown")},
                    params={"sentences": "abc"})
    assert r.status_code == 400


def test_table_rows_and_numbered_headings_are_avoided():
    text = ARTICLE + (
        "\n\n## Results\n\n3.2.3 Hive Weights Similarly to other gardens, the hive gained weight all summer. "
        "Hive A 27.3 38.1 3.3 4.5 1018\n\nHive B 22.0 31.4 2.9 3.8 990\n\n"
        "The garden hive produced more honey than the rooftop hive in every month we measured."
    )
    j = client.post("/summarize/text", json={"text": text, "sentences": 6}).json()
    picked = j["summary"] + " " + " ".join(j["key_points"])
    assert "27.3" not in picked and "22.0" not in picked
    assert "3.2.3" not in j["summary"]
