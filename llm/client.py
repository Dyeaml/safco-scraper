"""
llm/client.py
=============
The concrete LLM fallbacks behind the LLMExtractor / LLMClassifier interfaces.

WHERE THE LLM IS USED (and where it is NOT):

  USED — selectively, only when deterministic methods are exhausted:
    * extraction fallback: a /product/ page has NO usable JSON-LD (irregular or
      redesigned template). The model reads cleaned page text and returns
      structured JSON matching our schema.
    * classification tie-break: URL + DOM signals disagree or are inconclusive.

  NOT USED — for the ~99% deterministic path:
    * pages with JSON-LD (parsed directly — faster, free, deterministic),
    * routine navigation, dedup, storage.

GUARDRAILS:
    * hard per-run call cap (config: llm.max_calls_per_run) so a buggy run can't
      rack up cost,
    * temperature 0 for deterministic extraction,
    * strict "return ONLY JSON" prompting + defensive parsing (strip code
      fences, tolerate prose around the JSON),
    * the whole module is optional: if no API key is present, the interfaces
      simply aren't injected and the deterministic path runs alone.

The HTTP call uses the Anthropic Messages API. The key is read from the env via
LLMCfg.api_key (never stored in config.yaml).
"""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx
from bs4 import BeautifulSoup

from core.config import LLMCfg
from core.models import PageType

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"

# Trimmed field list the extractor needs the model to return.
_EXTRACT_SYSTEM = (
    "You extract structured product data from an e-commerce product page. "
    "Return ONLY a single JSON object, no prose, no code fences. "
    "Use these keys (null if absent): product_name, brand, sku, price (number), "
    "currency, availability (InStock/OutOfStock/Unknown), description, "
    "category_hierarchy (array of strings), image_urls (array of strings). "
    "Do not invent values; use null when the page does not state something."
)

_CLASSIFY_SYSTEM = (
    "You classify an e-commerce page into exactly one of: product, listing, "
    "category, other. 'product' = a single product detail page. 'listing'/"
    "'category' = a page listing multiple products or subcategories. "
    "Return ONLY one of those four words, lowercase, nothing else."
)


class CallCapExceeded(Exception):
    pass


class _Budget:
    """Shared per-run call counter enforcing the hard cap."""

    def __init__(self, max_calls: int):
        self.max_calls = max_calls
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.max_calls:
            raise CallCapExceeded(f"LLM call cap reached ({self.max_calls})")
        self.used += 1


class AnthropicLLM:
    """Thin synchronous client. The agents call it on the rare fallback path,
    so a blocking call there is acceptable and keeps the code simple."""

    def __init__(self, cfg: LLMCfg, budget: Optional[_Budget] = None):
        if not cfg.api_key:
            raise RuntimeError(
                f"LLM enabled but no API key in ${cfg.api_key_env}. "
                "Set it in .env or disable llm in config.yaml."
            )
        self.cfg = cfg
        self.budget = budget or _Budget(cfg.max_calls_per_run)
        self._client = httpx.Client(timeout=30.0)

    # ------------------------------------------------------------------ #
    def _complete(self, system: str, user: str, max_tokens: int) -> str:
        self.budget.spend()
        resp = self._client.post(
            _API_URL,
            headers={
                "x-api-key": self.cfg.api_key,
                "anthropic-version": _API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.cfg.model,
                "max_tokens": max_tokens,
                "temperature": self.cfg.temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        # Concatenate any text blocks in the response.
        return "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()

    # ------------------------------------------------------------------ #
    # LLMExtractor interface
    # ------------------------------------------------------------------ #
    def extract(self, html: str, source_url: str) -> dict:
        """Extraction fallback: return a dict matching the extractor's expected
        keys. Robust to the model wrapping JSON in prose or code fences."""
        text = _clean_html_to_text(html)
        user = f"URL: {source_url}\n\nPage content:\n{text[:6000]}"
        raw = self._complete(_EXTRACT_SYSTEM, user, self.cfg.max_tokens)
        return _parse_json_object(raw)

    # ------------------------------------------------------------------ #
    # LLMClassifier interface
    # ------------------------------------------------------------------ #
    def classify(self, url: str, html: str) -> PageType:
        text = _clean_html_to_text(html)
        user = f"URL: {url}\n\nPage content (truncated):\n{text[:2500]}"
        raw = self._complete(_CLASSIFY_SYSTEM, user, 16).lower()
        mapping = {
            "product": PageType.PRODUCT,
            "listing": PageType.LISTING,
            "category": PageType.CATEGORY,
            "other": PageType.OTHER,
        }
        for word, ptype in mapping.items():
            if word in raw:
                return ptype
        return PageType.OTHER

    def close(self) -> None:
        self._client.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean_html_to_text(html: str) -> str:
    """Strip scripts/styles and collapse to readable text, so we send the model
    meaningful content instead of markup (cheaper + better extraction)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)


def _parse_json_object(raw: str) -> dict:
    """Extract the first JSON object from a model response, tolerating code
    fences or surrounding prose."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Find the first {...} balanced-ish block.
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {}  # give up gracefully; extractor will produce a thin record


def build_llm(cfg: LLMCfg) -> Optional[AnthropicLLM]:
    """Factory: returns a client only if the LLM is enabled AND a key exists.
    Otherwise returns None and the pipeline runs deterministic-only."""
    if not cfg.is_usable:
        return None
    return AnthropicLLM(cfg)


__all__ = ["AnthropicLLM", "build_llm", "CallCapExceeded"]
