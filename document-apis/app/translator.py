"""Translation for the Doc Summarizer, using Azure Translator.

Settings (environment variables on the Render service):

    AZURE_TRANSLATOR_KEY        key of the free (F0) Translator resource. Used for PRO and ULTRA.
    AZURE_TRANSLATOR_KEY_MEGA   optional key of a paid (S1) resource. When set, MEGA callers use it
                                and get the bigger limits below. Leave unset until Mega is switched on.
    AZURE_TRANSLATOR_REGION     only needed if the resource was created in a region, not "Global".
    MEGA_MONTHLY_CHARS          characters one Mega customer may translate per month (default 2,000,000).

The free tier stops when its monthly allowance is used up; it never bills. The paid tier has no
built-in stop, which is why Mega customers get a monthly ceiling here.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
TIMEOUT = 30.0

FREE_DOC_CHARS = 30_000       # largest foreign document translated on PRO / ULTRA (about 10 pages)
MEGA_DOC_CHARS = 200_000      # largest foreign document translated on MEGA with a paid key
MEGA_MONTHLY_DEFAULT = 2_000_000

# Azure accepts at most 50,000 characters and 1,000 texts per request; stay well inside both.
BATCH_CHARS = 40_000
BATCH_ITEMS = 100

_LANG = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8}){0,2}$")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_ENGLISH = set("""
the and of to in is that it for with as was on are be this by at from or an have not but they which
you were their has had its can will would there been if more when who what about into than them
""".split())

# Characters used this month by each Mega customer. Kept in memory: it resets when the service
# restarts, so treat it as a guard against runaway use, not as billing.
_usage: dict[tuple[str, str], int] = {}


def _fail(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def _unavailable() -> HTTPException:
    return _fail(503, "translation_unavailable",
                 "Translation is unavailable right now. Summaries without translate_to still work.")


def reset_usage() -> None:
    _usage.clear()


def clean_language(value: str | None) -> str | None:
    """Return a tidy language code, or None when nothing was asked for. The RapidAPI playground
    sends '{}' for an empty optional field, so blank-looking values count as not set."""
    raw = (value or "").strip().lower().replace("_", "-")
    if raw in ("", "{}", "null", "none", "undefined"):
        return None
    if not _LANG.match(raw):
        raise _fail(400, "bad_language",
                    "translate_to should be a language code such as es, fr, de, ja or zh-hans.")
    return raw


def looks_english(text: str) -> bool:
    """Cheap local check, so English documents are never sent away just to find out what they are."""
    words = [w.lower() for w in _WORD.findall(text[:6000])]
    if len(words) < 5:
        return bool(words) and all(w.isascii() for w in words)
    common = sum(1 for w in words if w in _ENGLISH)
    return common / len(words) >= 0.15


@dataclass
class Access:
    key: str
    doc_chars: int               # biggest foreign document this caller may have translated
    monthly: int | None = None   # monthly character ceiling (paid key only)
    user: str = ""


def access_for(plan: str | None, user: str | None = None) -> Access:
    """Work out which key and limits a caller gets from their RapidAPI plan."""
    plan = (plan or "").strip().upper()
    if plan == "BASIC":
        raise _fail(403, "plan_required",
                    "Translation is included in the Pro and Ultra plans. Summaries without translate_to "
                    "work on every plan.")
    free = os.getenv("AZURE_TRANSLATOR_KEY", "").strip()
    paid = os.getenv("AZURE_TRANSLATOR_KEY_MEGA", "").strip()
    if plan == "MEGA" and paid:
        try:
            monthly = int(os.getenv("MEGA_MONTHLY_CHARS", "") or MEGA_MONTHLY_DEFAULT)
        except ValueError:
            monthly = MEGA_MONTHLY_DEFAULT
        return Access(paid, MEGA_DOC_CHARS, monthly, user or "unknown")
    if not free:
        raise _unavailable()
    return Access(free, FREE_DOC_CHARS)


def _month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def _reserve(access: Access, chars: int) -> None:
    if access.monthly is None:
        return
    slot = (access.user, _month())
    used = _usage.get(slot, 0)
    if used + chars > access.monthly:
        raise _fail(429, "translation_limit_reached",
                    f"This month's translation allowance of {access.monthly:,} characters is used up. "
                    "It resets at the start of next month.")
    for old in [k for k in _usage if k[1] != slot[1]]:
        del _usage[old]
    _usage[slot] = used + chars


def _pieces(text: str) -> list[str]:
    """Split one very long text at spaces so each piece fits in a request."""
    out = []
    while len(text) > BATCH_CHARS:
        cut = text.rfind(" ", 0, BATCH_CHARS)
        cut = cut if cut > 0 else BATCH_CHARS
        out.append(text[:cut])
        text = text[cut:].lstrip()
    out.append(text)
    return out


def _batches(texts: list[str]):
    batch, size = [], 0
    for t in texts:
        if batch and (size + len(t) > BATCH_CHARS or len(batch) >= BATCH_ITEMS):
            yield batch
            batch, size = [], 0
        batch.append(t)
        size += len(t)
    if batch:
        yield batch


async def _call(client: httpx.AsyncClient, access: Access, texts: list[str], to: str) -> list[dict]:
    headers = {"Ocp-Apim-Subscription-Key": access.key, "Content-Type": "application/json"}
    region = os.getenv("AZURE_TRANSLATOR_REGION", "").strip()
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    try:
        resp = await client.post(ENDPOINT, params={"api-version": "3.0", "to": to},
                                 headers=headers, json=[{"Text": t} for t in texts])
    except httpx.HTTPError:
        raise _unavailable()
    if resp.status_code == 200:
        return resp.json()
    try:
        code = int(resp.json()["error"]["code"])
    except Exception:
        code = resp.status_code * 1000
    # 400035/400036: the source or target language isn't one Azure supports.
    if code in (400035, 400036):
        raise _fail(400, "bad_language",
                    f"'{to}' isn't a language we can translate to. Use a code such as es, fr, de, ja or zh-hans.")
    # Anything else (allowance used up, bad key, rate limit, outage) is ours to fix, not the caller's.
    raise _unavailable()


async def translate(texts: list[str], to: str, access: Access) -> tuple[list[str], str | None, int]:
    """Translate texts into `to`. Returns (translations in the same order, the language Azure
    detected, characters sent). Empty strings are passed through without being sent."""
    spans, flat = [], []
    for t in texts:
        parts = _pieces(t) if t and t.strip() else []
        spans.append(len(parts))
        flat += parts
    chars = sum(len(p) for p in flat)
    if not flat:
        return list(texts), None, 0
    _reserve(access, chars)

    done: list[str] = []
    detected: str | None = None
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for batch in _batches(flat):
            for item in await _call(client, access, batch, to):
                done.append(item["translations"][0]["text"])
                detected = detected or (item.get("detectedLanguage") or {}).get("language")

    out, i = [], 0
    for original, n in zip(texts, spans):
        out.append(" ".join(done[i:i + n]) if n else original)
        i += n
    return out, detected, chars
