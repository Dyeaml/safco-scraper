"""
agents/extractor.py
===================
Turns a product page's HTML into a validated `Product`.

Strategy (verified against the real Safco markup):

  1. PRIMARY  — JSON-LD. Each product page embeds a `Product` block (name, sku,
     brand, description, price, currency, availability, images) and a
     `BreadcrumbList` block (category hierarchy). This is fast, clean, stable,
     and covers the majority of required fields.

  2. SECONDARY (DOM) — fill/cross-check fields JSON-LD may omit:
        * <h1> as a product-name fallback,
        * `.price` / `[data-price-amount]` as a price fallback,
        * spec tables / related-product links WHEN present in static HTML.
     On this site specs & related are JS-injected (absent from static HTML), so
     they come back empty here — that's expected, not an error.

  3. FALLBACK (LLM) — when a /product/ page has NO usable JSON-LD (irregular or
     redesigned template). The LLM reads the cleaned HTML/text and returns
     structured JSON matching our schema. Used selectively; every record stamps
     `extraction_method` so observability can report the LLM-fallback rate.

The extractor never raises on a single bad field; it records what it found and
lets the validator decide if the record is acceptable.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup

from core.config import ExtractionCfg
from core.models import (
    Availability,
    CategoryRef,
    ExtractionMethod,
    Product,
    ProductVariant,
)


class ExtractionError(Exception):
    """Raised only when a page yields nothing usable AND no LLM fallback ran."""


class Extractor:
    def __init__(self, cfg: ExtractionCfg, llm_extractor: Optional["LLMExtractor"] = None):
        self.cfg = cfg
        self.llm = llm_extractor  # optional; injected so the LLM is opt-in

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def extract(self, html: str, source_url: str) -> Product:
        """Extract a Product from page HTML. Tries JSON-LD, then DOM, then LLM."""
        soup = BeautifulSoup(html, "lxml")

        product_ld, breadcrumb_ld = self._find_jsonld(soup)

        if product_ld:
            product = self._from_jsonld(product_ld, breadcrumb_ld, source_url)
            method = ExtractionMethod.JSON_LD
        else:
            # No JSON-LD Product block — try LLM fallback if available.
            if self.llm is not None and self.cfg.use_llm_fallback:
                data = self.llm.extract(html=html, source_url=source_url)
                product = self._from_llm(data, breadcrumb_ld, source_url)
                method = ExtractionMethod.LLM_FALLBACK
            else:
                raise ExtractionError(
                    f"No JSON-LD Product block and no LLM fallback for {source_url}"
                )

        # DOM enrichment: fill gaps the structured source didn't cover.
        enriched = self._enrich_from_dom(product, soup)
        if enriched and method == ExtractionMethod.JSON_LD:
            method = ExtractionMethod.MIXED
        product.extraction_method = method
        return product

    # ------------------------------------------------------------------ #
    # JSON-LD primary path
    # ------------------------------------------------------------------ #
    def _find_jsonld(self, soup: BeautifulSoup) -> tuple[Optional[dict], Optional[dict]]:
        """Parse all JSON-LD scripts; return (Product dict, BreadcrumbList dict).

        Handles three real-world shapes: one object per <script>, multiple
        concatenated objects in one <script>, and @graph arrays.
        """
        product_type = self.cfg.jsonld_types.get("product", "Product")
        breadcrumb_type = self.cfg.jsonld_types.get("breadcrumb", "BreadcrumbList")
        product_ld = breadcrumb_ld = None

        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            for obj in self._loads_jsonld(raw):
                t = obj.get("@type")
                if t == product_type and product_ld is None:
                    product_ld = obj
                elif t == breadcrumb_type and breadcrumb_ld is None:
                    breadcrumb_ld = obj
        return product_ld, breadcrumb_ld

    @staticmethod
    def _loads_jsonld(raw: str) -> list[dict]:
        """Robustly load JSON-LD that may be a single object, an array, an
        @graph, or several objects concatenated in one tag."""
        raw = raw.strip()
        results: list[dict] = []
        try:
            data = json.loads(raw)
            candidates = data if isinstance(data, list) else [data]
            for c in candidates:
                if isinstance(c, dict) and "@graph" in c:
                    results.extend(g for g in c["@graph"] if isinstance(g, dict))
                elif isinstance(c, dict):
                    results.append(c)
            return results
        except json.JSONDecodeError:
            # Concatenated objects: }{ — split and retry each.
            for chunk in re.split(r"}\s*{", raw):
                chunk = chunk if chunk.startswith("{") else "{" + chunk
                chunk = chunk if chunk.endswith("}") else chunk + "}"
                try:
                    results.append(json.loads(chunk))
                except json.JSONDecodeError:
                    continue
        return results

    def _from_jsonld(self, p: dict, bc: Optional[dict], source_url: str) -> Product:
        offers = p.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        brand = p.get("brand")
        if isinstance(brand, dict):
            brand = brand.get("name")

        images = p.get("image") or []
        if isinstance(images, str):
            images = [images]
        if self.cfg.image_strip_query:
            images = [self._strip_query(u) for u in images]

        return Product(
            product_name=self._clean_text(p.get("name")) or "Unknown",
            sku=p.get("sku"),
            product_url=p.get("url") or p.get("@id", "").split("#")[0] or source_url,
            brand=self._clean_text(brand),
            description=self._clean_text(p.get("description")),
            category_hierarchy=self._categories_from_breadcrumb(bc),
            price=self._to_float(offers.get("price")),
            currency=offers.get("priceCurrency"),
            availability=Availability.from_schema_org(offers.get("availability")),
            image_urls=images,
            source_url=source_url,
        )

    def _categories_from_breadcrumb(self, bc: Optional[dict]) -> list[CategoryRef]:
        """Build the category hierarchy from a BreadcrumbList, dropping the
        configured roots (Home / Dental Supplies) and the leaf (the product)."""
        if not bc:
            return []
        items = bc.get("itemListElement", [])
        drop = set(self.cfg.breadcrumb_drop)
        refs: list[CategoryRef] = []
        for el in sorted(items, key=lambda e: e.get("position", 0)):
            name = el.get("name")
            url = el.get("item")
            if not name or name in drop:
                continue
            refs.append(CategoryRef(name=name, url=url))
        # The last breadcrumb is the product itself — drop it as a category.
        if refs:
            refs = refs[:-1]
        return refs

    # ------------------------------------------------------------------ #
    # DOM secondary path — enrich gaps
    # ------------------------------------------------------------------ #
    def _enrich_from_dom(self, product: Product, soup: BeautifulSoup) -> bool:
        """Fill missing fields from the DOM. Returns True if anything was added.

        Conservative: only fills fields that are currently empty, so JSON-LD
        (the more reliable source) always wins when present.
        """
        changed = False
        sel = self.cfg.selectors

        # Name fallback from <h1>
        if product.product_name in (None, "", "Unknown"):
            h1 = soup.find("h1")
            if h1:
                product.product_name = h1.get_text(strip=True)
                changed = True

        # Price fallback from .price / [data-price-amount]
        if product.price is None:
            price_el = soup.select_one("[data-price-amount]") or soup.select_one(".price")
            if price_el:
                amt = price_el.get("data-price-amount") or price_el.get_text(strip=True)
                product.price = self._to_float(amt)
                changed = product.price is not None or changed

        # Specifications table (absent on this site, present on others)
        spec_sel = sel.get("specifications_table")
        if spec_sel and not product.specifications:
            specs: dict[str, str] = {}
            for row in soup.select(spec_sel):
                k = row.select_one(sel.get("spec_row_key", "th"))
                v = row.select_one(sel.get("spec_row_value", "td"))
                if k and v:
                    specs[k.get_text(strip=True)] = v.get_text(strip=True)
            if specs:
                product.specifications = specs
                changed = True

        # Alternative / related products
        alt_sel = sel.get("alternative_products")
        if alt_sel and not product.alternative_products:
            urls = []
            for a in soup.select(alt_sel):
                href = a.get("href")
                if href and "/product/" in href:
                    urls.append(href)
            if urls:
                product.alternative_products = list(dict.fromkeys(urls))  # dedup, keep order
                changed = True

        return changed

    # ------------------------------------------------------------------ #
    # LLM fallback path
    # ------------------------------------------------------------------ #
    def _from_llm(self, data: dict, bc: Optional[dict], source_url: str) -> Product:
        """Build a Product from the LLM's structured JSON output."""
        return Product(
            product_name=self._clean_text(data.get("product_name")) or "Unknown",
            sku=data.get("sku"),
            product_url=data.get("product_url") or source_url,
            brand=self._clean_text(data.get("brand")),
            description=self._clean_text(data.get("description")),
            category_hierarchy=self._categories_from_breadcrumb(bc)
            or [CategoryRef(name=c) for c in (data.get("category_hierarchy") or [])],
            price=self._to_float(data.get("price")),
            currency=data.get("currency"),
            availability=Availability.from_schema_org(data.get("availability")),
            image_urls=data.get("image_urls") or [],
            source_url=source_url,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_text(value: Any) -> Optional[str]:
        """Unescape HTML entities (&bull; &nbsp; etc.), collapse whitespace."""
        if not value or not isinstance(value, str):
            return None
        text = unescape(value)
        text = text.replace("\xa0", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text or None

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        # Strip currency symbols, "From", thousands separators.
        m = re.search(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(m.group()) if m else None

    @staticmethod
    def _strip_query(url: str) -> str:
        """Drop query params from image URLs to get the original asset."""
        parts = urlparse(url)
        return urlunparse(parts._replace(query=""))


# --------------------------------------------------------------------------- #
# LLM extractor — thin, optional. Real client wired in agents/llm later; kept
# as a Protocol-friendly stub so the Extractor can be tested without an API key.
# --------------------------------------------------------------------------- #
class LLMExtractor:
    """Interface for the LLM-based extraction fallback.

    The concrete implementation (in the llm/ module) sends cleaned HTML to the
    model with a strict 'return only JSON matching this schema' prompt and
    parses the result. Defined here as the contract so the Extractor depends on
    the interface, not the implementation.
    """

    def extract(self, html: str, source_url: str) -> dict:  # pragma: no cover
        raise NotImplementedError


__all__ = ["Extractor", "LLMExtractor", "ExtractionError"]
