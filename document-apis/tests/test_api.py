"""Offline tests: every outside library is mocked with made-up sample data."""
import gzip

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import gutenberg, main
from app.main import app

client = TestClient(app)

CATALOG = """Text#,Type,Issued,Title,Language,Authors,Subjects,LoCC,Bookshelves
101,Text,2001-01-01,The Sample Way,en,"Writer, Old, 1800-1870; Helper, Kind, 1850-1920 [Translator]",Philosophy; Made-up topics,B,Philosophy
102,Text,2002-01-01,Still Protected,en,"Person, New, 1950-",Fiction,PS,
103,Sound,2003-01-01,The Sample Way (audio),en,"Writer, Old, 1800-1870",,,
""" + "".join(f"{2000+i},Text,2010-01-01,Filler Title {i},fr,\"Nobody, A\",,,\n" for i in range(1100))
RDF_PD = "<rdf><dcterms:rights>Public domain in the USA.</dcterms:rights></rdf>"
RDF_C = "<rdf><dcterms:rights>Copyrighted. Read the copyright notice inside this book for details.</dcterms:rights></rdf>"
OL = {"numFound": 2, "docs": [
    {"key": "/works/OL1W", "title": "The Sample Way", "author_name": ["Old Writer"], "first_publish_year": 1850,
     "ia": ["sampleway00writ"], "ebook_access": "public", "language": ["eng"], "cover_i": 42},
    {"key": "/works/OL2W", "title": "Lending Only", "author_name": ["New Person"], "ia": ["lend01"],
     "ebook_access": "borrowable"},
]}
LV = {"books": [{"id": "77", "title": "Sample Way, The", "description": "<p>An audio reading.</p>",
                 "url_text_source": "https://www.gutenberg.org/etext/101", "language": "English",
                 "copyright_year": "1850", "url_rss": "https://librivox.org/rss/77",
                 "url_zip_file": "https://archive.org/compress/x.zip", "url_librivox": "https://librivox.org/sample-way/",
                 "totaltime": "1:00:00", "authors": [{"first_name": "Old", "last_name": "Writer", "dob": "1800", "dod": "1870"}],
                 "sections": [{"title": "Part 1", "listen_url": "https://archive.org/1.mp3", "playtime": "600"}],
                 "genres": [{"name": "*Non-fiction"}], "translators": []}]}

RAW = """﻿The Project Gutenberg eBook of The Sample Way

Some license text here.

*** START OF THE PROJECT GUTENBERG EBOOK THE SAMPLE WAY ***

CHAPTER I

This is a made-up sentence that was
hard-wrapped across two lines.

Second paragraph here.

CHAPTER II

    A short verse line
    and another one

Final words.

*** END OF THE PROJECT GUTENBERG EBOOK THE SAMPLE WAY ***

More license text that should be removed.
"""


@pytest.fixture(autouse=True)
def fresh():
    main.search_cache.clear(); main.book_cache.clear(); main.text_cache.clear()
    gutenberg._books.clear(); gutenberg._by_id.clear(); gutenberg._rights.clear()
    gutenberg._loaded_at = 0


def mock_pg(router):
    router.get("https://www.gutenberg.org/cache/epub/feeds/pg_catalog.csv.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(CATALOG.encode())))
    router.get("https://www.gutenberg.org/cache/epub/101/pg101.rdf").mock(return_value=httpx.Response(200, text=RDF_PD))
    router.get("https://www.gutenberg.org/cache/epub/102/pg102.rdf").mock(return_value=httpx.Response(200, text=RDF_C))


def mock_all(router):
    mock_pg(router)
    router.get("https://openlibrary.org/search.json").mock(return_value=httpx.Response(200, json=OL))
    router.get("https://librivox.org/api/feed/audiobooks").mock(return_value=httpx.Response(200, json=LV))


@respx.mock
def test_search_merges_and_filters():
    mock_all(respx)
    r = client.get("/search", params={"q": "sample way"})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [b["id"] for b in body["results"]]
    assert ids == ["gutenberg:101"]  # 3 copies folded into one, copyrighted + borrowable dropped
    main_book = body["results"][0]
    assert {a["id"] for a in main_book["also_available"]} == {"openlibrary:OL1W", "librivox:77"}
    assert main_book["downloads"]["epub"].endswith("101.epub3.images")
    assert main_book["authors"][0] == {"name": "Writer, Old", "born": 1800, "died": 1870}
    assert main_book["translators"] == ["Helper, Kind"]
    assert body["sources"] == {"gutenberg": "1 found", "openlibrary": "1 found", "librivox": "1 found"}


@respx.mock
def test_audio_only():
    mock_all(respx)
    r = client.get("/search", params={"q": "sample", "type": "audio"})
    b = r.json()["results"]
    assert [x["id"] for x in b] == ["librivox:77"]
    assert b[0]["audio"]["chapters"][0]["seconds"] == 600
    assert b[0]["authors"][0] == {"name": "Old Writer", "born": 1800, "died": 1870}


@respx.mock
def test_one_source_down_still_works():
    respx.get(url__startswith="https://www.gutenberg.org/").mock(side_effect=httpx.ConnectTimeout("slow"))
    respx.get("https://openlibrary.org/search.json").mock(return_value=httpx.Response(200, json=OL))
    respx.get("https://librivox.org/api/feed/audiobooks").mock(return_value=httpx.Response(404))
    r = client.get("/search", params={"q": "sample"})
    assert r.status_code == 200
    j = r.json()
    assert j["sources"]["gutenberg"] == "unavailable" and j["sources"]["librivox"] == "0 found"
    assert j["results"][0]["id"] == "openlibrary:OL1W"
    assert j["results"][0]["read_online"] == "https://archive.org/details/sampleway00writ"


def test_search_needs_query():
    assert client.get("/search").status_code == 400
    assert client.get("/search", params={"q": "x", "source": "scribd"}).status_code == 400


@respx.mock
def test_book_and_copyright_block():
    mock_pg(respx)
    assert client.get("/book/gutenberg:101").json()["title"] == "The Sample Way"
    assert client.get("/book/gutenberg:102").status_code == 451
    assert client.get("/book/nothing").status_code == 400


@respx.mock
def test_read_clean_text():
    mock_pg(respx)
    respx.get("https://www.gutenberg.org/ebooks/101.txt.utf-8").mock(return_value=httpx.Response(200, text=RAW))
    r = client.get("/read/gutenberg:101", params={"words_per_page": 200})
    assert r.status_code == 200, r.text
    j = r.json()
    text = " ".join(j["content"])
    assert "license" not in text.lower() and "PROJECT GUTENBERG" not in text
    assert "This is a made-up sentence that was hard-wrapped across two lines." in j["content"]
    assert "A short verse line\nand another one" in j["content"]
    assert [c["title"] for c in j["chapters"]] == ["CHAPTER I", "CHAPTER II"]
    assert j["total_pages"] == 1 and j["next_page"] is None


def test_proxy_secret(monkeypatch):
    monkeypatch.setattr(main, "PROXY_SECRET", "s3cret")
    assert client.get("/search", params={"q": "x"}).status_code == 403
    assert client.get("/health").status_code == 200


@respx.mock
def test_catalog_search_ranking_and_filters():
    mock_pg(respx)
    r = client.get("/search", params={"q": "sample way", "source": "gutenberg"}).json()
    assert [b["id"] for b in r["results"]] == ["gutenberg:101"]  # audio row skipped
    r = client.get("/search", params={"q": "protected", "source": "gutenberg"}).json()
    assert r["results"] == []  # copyrighted per its RDF
    r = client.get("/search", params={"author": "old writer", "source": "gutenberg"}).json()
    assert r["results"][0]["id"] == "gutenberg:101"
    r = client.get("/search", params={"q": "made-up topics", "source": "gutenberg"}).json()
    assert r["results"][0]["id"] == "gutenberg:101"  # subject match
    r = client.get("/search", params={"q": "sample", "language": "fr", "source": "gutenberg"}).json()
    assert r["results"] == []


def test_head_root():
    assert client.head("/").status_code == 200
