# Document APIs

One server for three document APIs (each its own RapidAPI listing):
1. **Public Domain Book Finder**: `/search`, `/book/{id}`, `/read/{id}`
2. **Clean PDF Reader**: `/pdf/read`, `/pdf/chapters`, `/pdf/info`
3. **Doc Summarizer**: `/summarize/text`, `/summarize/url`, `/summarize/file`

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

## Doc Summarizer API

Summarize any text, web page or PDF. It picks the most important sentences straight from the
document (extractive summarizing), so it needs no AI model, costs nothing extra to run and never
adds anything that isn't in the text.

| Endpoint | Input |
|---|---|
| `POST /summarize/text` | JSON: `{"text": "...", "length": "short"}` |
| `GET /summarize/url?url=...` | A web page, a PDF link or a plain-text link |
| `POST /summarize/file` | Multipart upload: a PDF, `.txt` or `.md` file |

Options: `length` (`short` 3 / `medium` 5 / `long` 8 sentences), `sentences` (1–30, overrides
length), `key_points` (0–15, default 5), `keywords` (0–30, default 10), `by_section` (default true).

Every response has `summary`, `key_points`, `keywords`, `word_count`, `reading_time_minutes` and,
when the document has chapters or headings, `sections` with a short summary of each one.
PDFs reuse the Clean PDF Reader parser, so headers, footers and page numbers are already gone.

### Translation (`translate_to`)

Add `translate_to` with a language code (`es`, `fr`, `de`, `ja`, `zh-hans`, ...) to any of the three
endpoints to get the summary, key points, keywords and section summaries in that language.

- **English document, another language wanted:** only the result is translated.
- **Document that isn't in English:** the whole document is translated to English first, then
  summarized. Limit: 30,000 characters (about 10 pages).
- The response gains a `translation` block: `source_language`, `target_language`, `characters_used`.
- Translation is for the Pro plan and up. Basic callers get `403 plan_required`; their normal
  summaries are unaffected.
- If translation can't run (no key set, or the month's free allowance is used up) the caller gets
  `503 translation_unavailable`. Requests without `translate_to` keep working.

Translation uses Azure Translator. The free tier gives 2 million characters a month and stops,
without billing, when they run out.

## Run locally
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload
```

## Environment variables
- `RAPIDAPI_PROXY_SECRET`: when set, only requests coming through RapidAPI are accepted (`/health` stays open).
- `CONTACT_EMAIL`: added to the User-Agent, which Open Library asks for.
- `AZURE_TRANSLATOR_KEY`: key of the free (F0) Azure Translator resource. Without it, `translate_to` returns 503.
- `AZURE_TRANSLATOR_REGION`: only if the Azure resource was created in a region, not "Global" (e.g. `westus2`).
- `AZURE_TRANSLATOR_KEY_MEGA`: optional. Key of a paid (S1) Azure resource. When set, Mega callers use it,
  can translate documents up to 200,000 characters, and no longer share the free allowance.
- `MEGA_MONTHLY_CHARS`: optional. Characters one Mega customer may translate per month (default 2,000,000).
  The count is held in memory and resets if the service restarts, so also set a budget alert in Azure.
