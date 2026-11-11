# Document APIs

One server for three document APIs (each its own RapidAPI listing):
1. **Public Domain Book Finder** (live first): `/search`, `/book/{id}`, `/read/{id}`
2. Clean PDF Reader (coming next)
3. Doc Summarizer / Chapter Splitter (coming last)

## Public Domain Book Finder API

Search **3 free, legal libraries at once** and read public-domain books as clean text. No ads, pop-ups or sign-in.

| Library | What it gives you |
|---|---|
| Project Gutenberg | 70,000+ public-domain ebooks (EPUB, Kindle, HTML, plain text) |
| Open Library / Internet Archive | Scanned books with **full public access** (lending-library titles are left out) |
| LibriVox | Free public-domain audiobooks read by volunteers |

Only public-domain or fully open titles are returned. Books still under copyright are filtered out.

## Endpoints (3)

### `GET /search`
| Param | Example | Notes |
|---|---|---|
| `q` | `tao te ching` | Title, keywords or topic |
| `author` | `Lao Tzu` | Optional |
| `language` | `en`, `es`, `Spanish` | Optional |
| `type` | `all` / `text` / `audio` | Default `all` |
| `source` | `gutenberg,librivox` | Default `all` |
| `limit` | `9` | 1–30, default 9 |

When the same book shows up in more than one library, it's merged into one result, and the other copies are listed under `also_available`.

### `GET /book/{id}`
Gives the details and every free format for one book. Example ids: `gutenberg:216`, `openlibrary:OL123W`, `librivox:419`.

### `GET /read/{id}?page=1&words_per_page=900`
The clean reader, for Project Gutenberg books. It removes the license header and footer, re-joins the broken line wraps into real paragraphs, lists the chapters, and serves the book a page at a time.

## Run locally
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload
```

## Environment variables
- `RAPIDAPI_PROXY_SECRET`: when set, only requests coming through RapidAPI are accepted (`/health` stays open).
- `CONTACT_EMAIL`: added to the User-Agent, which Open Library asks for.
