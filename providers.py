"""Translation providers.

Every provider implements ``translate_batch(texts, source, target) -> list[str]``.
Pick one with the TRANSLATION_PROVIDER env var ("google" or "mock").
"""
from __future__ import annotations

import os
import re
import time
import unicodedata
from typing import Protocol

import httpx


class TranslationError(RuntimeError):
    pass


class TranslationProvider(Protocol):
    name: str

    def translate_batch(self, texts: list[str], source: str, target: str) -> list[str]:
        ...


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class GoogleProvider:
    """Google Cloud Translation v2 (Basic) REST API."""

    name = "google"
    URL = "https://translation.googleapis.com/language/translate/v2"
    MAX_SEGMENTS = 128          # API limit per request
    MAX_CHARS = 25_000          # stay under the recommended 30k per request

    def __init__(self, api_key: str | None = None, timeout: float = 60.0):
        self.api_key = api_key or os.environ.get("GOOGLE_TRANSLATE_API_KEY", "")
        if not self.api_key:
            raise TranslationError("GOOGLE_TRANSLATE_API_KEY is not set")
        self.client = httpx.Client(timeout=timeout)

    def translate_batch(self, texts: list[str], source: str, target: str) -> list[str]:
        out: list[str] = []
        chunk: list[str] = []
        size = 0
        for t in texts:
            if chunk and (len(chunk) >= self.MAX_SEGMENTS or size + len(t) > self.MAX_CHARS):
                out.extend(self._request(chunk, source, target))
                chunk, size = [], 0
            chunk.append(t)
            size += len(t)
        if chunk:
            out.extend(self._request(chunk, source, target))
        return out

    def _request(self, texts: list[str], source: str, target: str) -> list[str]:
        payload = {"q": texts, "source": source, "target": target, "format": "text"}
        delay = 1.0
        for attempt in range(5):
            try:
                r = self.client.post(self.URL, params={"key": self.api_key}, json=payload)
            except httpx.HTTPError as e:
                err = f"network error: {e}"
            else:
                if r.status_code == 200:
                    items = r.json()["data"]["translations"]
                    if len(items) != len(texts):
                        raise TranslationError("Google returned a different number of segments")
                    return [nfc(i["translatedText"]) for i in items]
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise TranslationError(f"Google Translate error {r.status_code}: {r.text[:300]}")
                err = f"HTTP {r.status_code}"
            if attempt == 4:
                raise TranslationError(f"Google Translate failed after retries ({err})")
            time.sleep(delay)
            delay *= 2
        raise AssertionError("unreachable")


_SAMPLES = {
    "te": (
        "ఈ పత్రం యొక్క అనువాదం తెలుగు భాషలో ఇక్కడ చూపబడింది. మేము అసలు లేఅవుట్‌ను "
        "అలాగే ఉంచుతూ ప్రతి పేరాను జాగ్రత్తగా అనువదిస్తాము. ఈ వాక్యం పరీక్ష కోసం "
        "మాత్రమే ఉపయోగించబడుతుంది మరియు నిజమైన అనువాదం కాదు. పట్టికలు, చిత్రాలు "
        "మరియు శీర్షికలు వాటి స్థానంలోనే ఉంటాయి."
    ),
    "hi": (
        "इस दस्तावेज़ का अनुवाद यहाँ हिंदी भाषा में दिखाया गया है। हम मूल लेआउट को "
        "बनाए रखते हुए हर अनुच्छेद का सावधानी से अनुवाद करते हैं। यह वाक्य केवल परीक्षण "
        "के लिए उपयोग किया जाता है और वास्तविक अनुवाद नहीं है। तालिकाएँ, चित्र और "
        "शीर्षक अपनी जगह पर बने रहते हैं।"
    ),
}

_NUMBER_TOKEN = re.compile(r"\d[\d.,%:/-]*")


class MockProvider:
    """Returns fixed Telugu/Hindi sample text of roughly the same length as the input.

    Numbers found in the source are kept (as a real translation would), so the
    renderer is also exercised on mixed-script text.
    """

    name = "mock"

    def __init__(self, expansion: float = 1.0):
        self.expansion = expansion      # >1 simulates translations longer than the source

    def translate_batch(self, texts: list[str], source: str, target: str) -> list[str]:
        if target not in _SAMPLES:
            raise TranslationError(f"unsupported target language: {target}")
        words = _SAMPLES[target].split()
        out = []
        for text in texts:
            numbers = _NUMBER_TOKEN.findall(text)
            budget = max(int(len(text) * self.expansion) - sum(len(n) + 1 for n in numbers), 1)
            picked: list[str] = []
            length = 0
            i = 0
            while length < budget or not picked:
                w = words[i % len(words)]
                picked.append(w)
                length += len(w) + 1
                i += 1
            out.append(nfc(" ".join(picked + numbers)))
        return out


def get_provider(name: str | None = None) -> TranslationProvider:
    name = (name or os.environ.get("TRANSLATION_PROVIDER") or "mock").strip().lower()
    if name == "google":
        return GoogleProvider()
    if name == "mock":
        return MockProvider()
    raise TranslationError(f"unknown TRANSLATION_PROVIDER: {name!r}")
