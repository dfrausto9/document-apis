"""Tests for the translate_to option on the Doc Summarizer.

Azure Translator is faked with respx, so these never touch the network or spend characters.
Run with: pytest tests/test_translation.py
"""

import json
import os
import sys

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import summarizer, translator  # noqa: E402
from tests.test_summarizer import ARTICLE  # noqa: E402

app = FastAPI()
app.include_router(summarizer.router)
client = TestClient(app)

AZURE = "https://api.cognitive.microsofttranslator.com/translate"
PRO = {"X-RapidAPI-Subscription": "PRO", "X-RapidAPI-User": "pat"}
MEGA = {"X-RapidAPI-Subscription": "MEGA", "X-RapidAPI-User": "morgan"}

# A Spanish document with the same shape as ARTICLE, and the English each piece "translates" to.
ES_TO_EN = {
    "Criar abejas en un jardín pequeño": "Keeping Honey Bees in a Small Garden",
    "Las abejas pueden prosperar en un jardín pequeño cuando la colmena se coloca con cuidado. "
    "Un lugar soleado y protegido del viento mantiene la colonia activa. "
    "La mayoría de los principiantes empieza con una sola colmena y un mentor.":
        "Honey bees can thrive in a small garden when the hive is placed with care. A sunny spot that "
        "is sheltered from strong wind keeps the colony warm and busy. Most beginners start with a "
        "single hive and a mentor who has kept bees for several years.",
    "Elegir una colmena": "Choosing a hive",
    "La colmena Langstroth es la opción más común porque sus cuadros son fáciles de revisar. "
    "Las colmenas de barra superior cuestan menos, pero guardan menos miel. "
    "La colmena debe mirar lejos de los caminos para no cruzarse con la gente.":
        "The Langstroth hive is the most common choice because its frames are easy to inspect. Top bar "
        "hives cost less to build, but they hold less honey. Whatever you pick, the hive should face "
        "away from paths so the flight line does not cross where people walk.",
    "Cosechar la miel": "Harvesting honey",
    "La miel está lista cuando la mayor parte del panal está sellada con cera. "
    "Tome solo el excedente y deje a las abejas suficiente miel para el invierno. "
    "Una colmena pequeña puede dar entre diez y quince kilos de miel en un buen año.":
        "Honey is ready when most of the comb is capped with wax. Take only the surplus, and leave the "
        "bees enough honey to survive the winter. A small garden hive can give twenty to thirty pounds "
        "of honey in a good year.",
}
_es = list(ES_TO_EN)
SPANISH = f"# {_es[0]}\n\n{_es[1]}\n\n## {_es[2]}\n\n{_es[3]}\n\n## {_es[4]}\n\n{_es[5]}\n"


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "free-key")
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY_MEGA", raising=False)
    monkeypatch.delenv("AZURE_TRANSLATOR_REGION", raising=False)
    translator.reset_usage()


def fake_azure(detected="en"):
    """Fake Azure: known Spanish pieces come back as their English, anything else gets a
    '[to] ' prefix. Records what was sent so tests can check what we paid for."""
    sent = {"texts": [], "keys": [], "to": []}

    def reply(request: httpx.Request):
        to = request.url.params["to"]
        texts = [item["Text"] for item in json.loads(request.content)]
        sent["texts"] += texts
        sent["keys"].append(request.headers["Ocp-Apim-Subscription-Key"])
        sent["to"].append(to)
        return httpx.Response(200, json=[
            {"detectedLanguage": {"language": detected, "score": 1.0},
             "translations": [{"text": ES_TO_EN.get(t, f"[{to}] {t}"), "to": to}]}
            for t in texts
        ])

    respx.post(AZURE).mock(side_effect=reply)
    return sent


def post_text(text=ARTICLE, headers=PRO, **options):
    return client.post("/summarize/text", json={"text": text, "length": "short", **options}, headers=headers)


# ---- English document, summary wanted in another language ----------------

@respx.mock
def test_summary_comes_back_in_the_requested_language():
    fake_azure()
    j = post_text(translate_to="es", key_points=2).json()
    assert j["summary"].startswith("[es] ")
    assert all(p.startswith("[es] ") for p in j["key_points"])
    assert j["title"] == "[es] Keeping Honey Bees in a Small Garden"
    assert j["sections"][1]["title"] == "[es] Choosing a hive"
    assert j["sections"][1]["summary"].startswith("[es] ")
    assert j["translation"]["source_language"] == "en"
    assert j["translation"]["target_language"] == "es"


@respx.mock
def test_only_the_summary_is_sent_for_an_english_document():
    sent = fake_azure()
    j = post_text(translate_to="fr", key_points=0, keywords=0, by_section=False).json()
    assert len(sent["texts"]) == 2          # the title and the summary, not the document
    paid = sum(len(t) for t in sent["texts"])
    assert 0 < paid < len(ARTICLE) / 2
    assert j["translation"]["characters_used"] == paid
    assert sent["keys"] == ["free-key"]


@respx.mock
def test_without_translate_to_nothing_changes():
    route = respx.post(AZURE).mock(return_value=httpx.Response(500))
    plain = client.post("/summarize/text", json={"text": ARTICLE, "length": "short"})
    assert plain.status_code == 200
    assert "translation" not in plain.json()
    assert not route.called


@respx.mock
def test_playground_blank_value_counts_as_not_set():
    route = respx.post(AZURE).mock(return_value=httpx.Response(500))
    r = client.get("/summarize/url", params={"url": "http://localhost/x", "translate_to": "{}"})
    assert r.status_code != 500 and not route.called
    assert summarizer._language("{}") is None and summarizer._language("  ") is None


@respx.mock
def test_english_document_to_english_costs_nothing():
    sent = fake_azure()
    j = post_text(translate_to="en").json()
    assert sent["texts"] == []
    assert j["translation"] == {"source_language": "en", "target_language": "en", "characters_used": 0}
    assert not j["summary"].startswith("[")


# ---- Foreign document ----------------------------------------------------

@respx.mock
def test_foreign_document_is_translated_before_summarizing():
    sent = fake_azure(detected="es")
    r = post_text(SPANISH, translate_to="en")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["title"] == "Keeping Honey Bees in a Small Garden"
    assert "abejas" not in j["summary"] and "hive" in j["summary"].lower()
    assert [s["title"] for s in j["sections"]][1:] == ["Choosing a hive", "Harvesting honey"]
    assert j["translation"]["source_language"] == "es"
    assert j["translation"]["characters_used"] == sum(len(t) for t in sent["texts"])
    assert set(sent["to"]) == {"en"}


@respx.mock
def test_foreign_document_to_a_third_language_translates_twice():
    sent = fake_azure(detected="es")
    j = post_text(SPANISH, translate_to="de").json()
    assert j["summary"].startswith("[de] ")
    assert sent["to"][0] == "en" and sent["to"][-1] == "de"
    assert j["translation"]["source_language"] == "es"


@respx.mock
def test_foreign_document_over_the_cap_is_refused_before_spending():
    sent = fake_azure(detected="es")
    long_spanish = SPANISH + ("\n\n" + _es[1]) * 150      # well over 30,000 characters
    r = post_text(long_spanish, translate_to="en")
    assert r.status_code == 413
    assert r.json()["detail"]["error"] == "too_long_to_translate"
    assert "30,000" in r.json()["detail"]["message"]
    assert sent["texts"] == []


def test_english_and_foreign_text_are_told_apart():
    assert translator.looks_english(ARTICLE)
    assert not translator.looks_english(SPANISH)
    assert not translator.looks_english("这是一份关于养蜂的中文文件。蜜蜂在小花园里也能生活得很好。" * 5)


# ---- Plans ---------------------------------------------------------------

@respx.mock
def test_basic_plan_is_told_translation_needs_a_paid_plan():
    sent = fake_azure()
    r = post_text(translate_to="es", headers={"X-RapidAPI-Subscription": "BASIC"})
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "plan_required"
    assert sent["texts"] == []


@respx.mock
def test_mega_uses_the_paid_key_and_a_higher_cap(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY_MEGA", "paid-key")
    sent = fake_azure(detected="es")
    long_spanish = SPANISH + ("\n\n" + _es[1]) * 150
    r = post_text(long_spanish, translate_to="en", headers=MEGA)
    assert r.status_code == 200, r.text
    assert set(sent["keys"]) == {"paid-key"}
    assert len(sent["keys"]) > 1          # too big for one Azure request, so it went in batches
    assert r.json()["translation"]["characters_used"] > 30_000


@respx.mock
def test_mega_without_a_paid_key_falls_back_to_the_free_tier():
    sent = fake_azure()
    r = post_text(translate_to="es", headers=MEGA)
    assert r.status_code == 200
    assert sent["keys"] == ["free-key"]


@respx.mock
def test_mega_customer_stops_at_the_monthly_ceiling(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY_MEGA", "paid-key")
    monkeypatch.setenv("MEGA_MONTHLY_CHARS", "1500")
    sent = fake_azure()
    assert post_text(translate_to="es", headers=MEGA).status_code == 200
    spent = len(sent["texts"])
    while (r := post_text(translate_to="es", headers=MEGA)).status_code == 200:
        assert len(sent["texts"]) < 500, "ceiling never reached"
    assert r.status_code == 429
    assert r.json()["detail"]["error"] == "translation_limit_reached"
    before = len(sent["texts"])
    assert before >= spent
    # A different Mega customer has their own allowance.
    other = {**MEGA, "X-RapidAPI-User": "someone-else"}
    assert post_text(translate_to="es", headers=other).status_code == 200


# ---- Things going wrong --------------------------------------------------

@respx.mock
def test_no_key_configured_gives_a_clear_message(monkeypatch):
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY")
    r = post_text(translate_to="es")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "translation_unavailable"


@respx.mock
def test_free_allowance_used_up_gives_a_clear_message():
    respx.post(AZURE).mock(return_value=httpx.Response(403, json={"error": {
        "code": 403001, "message": "The operation isn't allowed because the subscription has exceeded its free quota."}}))
    r = post_text(translate_to="es")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "translation_unavailable"
    assert "free-key" not in r.text


@respx.mock
def test_azure_unreachable_gives_a_clear_message():
    respx.post(AZURE).mock(side_effect=httpx.ConnectError("down"))
    r = post_text(translate_to="es")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "translation_unavailable"


@respx.mock
def test_language_azure_does_not_know_is_a_caller_error():
    respx.post(AZURE).mock(return_value=httpx.Response(400, json={"error": {
        "code": 400036, "message": "The target language is not valid."}}))
    r = post_text(translate_to="zz")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_language"


def test_nonsense_language_code_is_rejected_without_calling_azure():
    r = post_text(translate_to="spanish please")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "bad_language"


@respx.mock
def test_region_header_is_sent_when_set(monkeypatch):
    monkeypatch.setenv("AZURE_TRANSLATOR_REGION", "westus2")
    route = respx.post(AZURE).mock(side_effect=lambda req: httpx.Response(200, json=[
        {"detectedLanguage": {"language": "en", "score": 1}, "translations": [{"text": "x", "to": "es"}]}
        for _ in json.loads(req.content)]))
    assert post_text(translate_to="es").status_code == 200
    assert route.calls.last.request.headers["Ocp-Apim-Subscription-Region"] == "westus2"


# ---- The other two endpoints ---------------------------------------------

@respx.mock
def test_file_upload_accepts_translate_to():
    fake_azure()
    r = client.post("/summarize/file", params={"translate_to": "es", "length": "short"}, headers=PRO,
                    files={"file": ("bees.md", ARTICLE.encode(), "text/markdown")})
    assert r.status_code == 200, r.text
    assert r.json()["summary"].startswith("[es] ")
    assert r.json()["source"]["filename"] == "bees.md"


@respx.mock
def test_url_accepts_translate_to(monkeypatch):
    fake_azure()
    monkeypatch.setattr(summarizer, "_check_public_host", lambda url: None)
    respx.get("https://example.com/bees.txt").mock(
        return_value=httpx.Response(200, text=ARTICLE, headers={"content-type": "text/plain"}))
    r = client.get("/summarize/url", headers=PRO,
                   params={"url": "https://example.com/bees.txt", "translate_to": "es", "length": "short"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"].startswith("[es] ")
