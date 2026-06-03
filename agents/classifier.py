"""
agents/classifier.py
====================
Decides what a fetched page IS, so the orchestrator can route it:

    PRODUCT              -> hand to the Extractor
    CATEGORY/SUBCATEGORY -> hand to the Navigator
    LISTING              -> hand to the Navigator
    OTHER                -> skip

Design: deterministic signals first, LLM only as a tie-breaker.

  1. URL pattern (cheap, from config):
        /product/                -> PRODUCT
        /catalog/{a}             -> CATEGORY
        /catalog/{a}/{b}         -> SUBCATEGORY
  2. DOM/JSON-LD signal (confirms the guess):
        a JSON-LD `Product` block          -> PRODUCT
        a JSON-LD `ItemList`/`CollectionPage` -> LISTING/category
  3. LLM tie-break (optional): only when 1 and 2 disagree or both are
     inconclusive AND the LLM is enabled. This keeps the LLM off the hot path
     for the ~99% of pages the rules classify confidently — using it as
     judgment where it adds value, not decoration.

Returns a ClassificationResult carrying the decision, a confidence, and the
signal that drove it, so the run is debuggable and the LLM-usage is observable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from core.config import ClassifierCfg
from core.models import PageType


@dataclass
class ClassificationResult:
    page_type: PageType
    confidence: float          # 0..1
    signal: str                # what drove the decision (for logging/debug)
    used_llm: bool = False


class Classifier:
    def __init__(self, cfg: ClassifierCfg, llm_classifier: Optional["LLMClassifier"] = None):
        self.cfg = cfg
        self.llm = llm_classifier
        # Pre-compile URL patterns from config.
        self._patterns = {
            ptype: re.compile(pat) for ptype, pat in cfg.url_patterns.items()
        }

    # ------------------------------------------------------------------ #
    def classify(self, url: str, html: Optional[str] = None) -> ClassificationResult:
        """Classify a page from its URL and (optionally) its HTML."""
        url_guess = self._classify_by_url(url)
        dom_guess = self._classify_by_dom(html) if html else None

        # Both agree -> high confidence, no LLM needed.
        if dom_guess and url_guess == dom_guess:
            return ClassificationResult(url_guess, 0.99, "url+dom agree")

        # DOM has a definitive Product/ItemList signal -> trust it over URL.
        if dom_guess in (PageType.PRODUCT, PageType.LISTING):
            return ClassificationResult(dom_guess, 0.9, f"dom signal ({dom_guess.value})")

        # Only the URL gave a confident answer.
        if url_guess != PageType.OTHER and dom_guess is None:
            return ClassificationResult(url_guess, 0.8, "url pattern (no html)")
        if url_guess != PageType.OTHER:
            return ClassificationResult(url_guess, 0.7, "url pattern")

        # Inconclusive -> LLM tie-break, if available and enabled.
        if self.cfg.use_llm_fallback and self.llm is not None and html:
            page_type = self.llm.classify(url=url, html=html)
            return ClassificationResult(page_type, 0.6, "llm tie-break", used_llm=True)

        return ClassificationResult(PageType.OTHER, 0.3, "no signal")

    # ------------------------------------------------------------------ #
    def _classify_by_url(self, url: str) -> PageType:
        path = urlparse(url).path
        # Order matters: subcategory pattern is more specific than category.
        if "product" in self._patterns and self._patterns["product"].search(path):
            return PageType.PRODUCT
        if "subcategory" in self._patterns and self._patterns["subcategory"].search(path):
            return PageType.SUBCATEGORY
        if "category" in self._patterns and self._patterns["category"].search(path):
            return PageType.CATEGORY
        return PageType.OTHER

    def _classify_by_dom(self, html: str) -> Optional[PageType]:
        """Look for definitive JSON-LD @type signals without full parsing.

        A Product block => product page. An ItemList/CollectionPage => a
        listing/category page. We scan the JSON-LD script contents for the
        @type tokens — cheap and robust to markup changes.
        """
        soup = BeautifulSoup(html, "lxml")
        has_product = has_list = False
        product_type = self.cfg.product_jsonld_type  # "Product"

        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text() or ""
            types = set(re.findall(r'"@type"\s*:\s*"([^"]+)"', raw))
            if product_type in types:
                # A standalone Product block (product page) vs Product items
                # inside an ItemList (listing). If ItemList is also present, the
                # Products are listing items, so it's a listing.
                if "ItemList" in types or "CollectionPage" in types:
                    has_list = True
                else:
                    has_product = True
            if "ItemList" in types or "CollectionPage" in types:
                has_list = True

        if has_product and not has_list:
            return PageType.PRODUCT
        if has_list:
            return PageType.LISTING
        return None


# --------------------------------------------------------------------------- #
# LLM tie-breaker interface — concrete impl lives in the llm/ module. Defined
# here so Classifier depends on the contract, not the implementation, and can
# be tested with the LLM absent.
# --------------------------------------------------------------------------- #
class LLMClassifier:
    def classify(self, url: str, html: str) -> PageType:  # pragma: no cover
        raise NotImplementedError


__all__ = ["Classifier", "ClassificationResult", "LLMClassifier"]
