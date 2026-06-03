"""
agents/navigator.py
==================
Discovers what to crawl. Given a category/listing page's HTML, it returns:

  * subcategory URLs   — more category pages to traverse (recurse), and
  * product references  — product detail URLs to enqueue for the Extractor,
                          each carrying the partial data already present on the
                          listing (name, sku, price, image) as a fallback.

Why JSON-LD instead of scraping the visible grid:
  Safco's listing pages render the product grid with JavaScript, so the static
  HTML's visible <a href="/product/..."> links are nearly empty. BUT the page
  embeds two JSON-LD ItemLists:
      1. CollectionPage items  -> subcategories
      2. Product items         -> the products on this page
  These are reliable, structured, and present without JS — so the navigator
  reads them directly. This is the same JSON-LD-first principle the Extractor
  uses, applied to navigation.

Pagination: Safco's static HTML exposes no ?p= / rel=next, so the ItemList is
the full set for a (sub)category. For sites that DO paginate, a config-driven
`pagination_next` selector / URL pattern is supported and documented; it's a
no-op here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from core.config import ExtractionCfg, SiteCfg


@dataclass
class ProductRef:
    """A product discovered on a listing page.

    `url` is what gets enqueued for detail extraction. The remaining fields are
    the partial data already available on the listing — kept so that if the
    detail-page fetch later fails, we can still persist a usable (if thinner)
    record instead of losing the product entirely.
    """
    url: str
    name: Optional[str] = None
    sku: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    image: Optional[str] = None


@dataclass
class NavigationResult:
    subcategory_urls: list[str] = field(default_factory=list)
    product_refs: list[ProductRef] = field(default_factory=list)
    next_page_url: Optional[str] = None


class Navigator:
    def __init__(self, site_cfg: SiteCfg, extraction_cfg: ExtractionCfg):
        self.site = site_cfg
        self.cfg = extraction_cfg
        self._allowed = set(site_cfg.allowed_domains)

    # ------------------------------------------------------------------ #
    def discover(self, html: str, page_url: str) -> NavigationResult:
        """Parse a listing page into subcategory URLs + product refs."""
        soup = BeautifulSoup(html, "lxml")
        result = NavigationResult()

        subcats: list[str] = []
        products: list[ProductRef] = []

        for obj in self._iter_jsonld(soup):
            if obj.get("@type") != "ItemList":
                continue
            elements = obj.get("itemListElement", [])
            for el in elements:
                item = el.get("item", el)  # item may be nested or inline
                if not isinstance(item, dict):
                    continue
                itype = item.get("@type")
                if itype == "CollectionPage":
                    url = self._norm(item.get("url"))
                    if url and self._in_scope(url):
                        subcats.append(url)
                elif itype == "Product":
                    ref = self._product_ref(item)
                    if ref and self._in_scope(ref.url):
                        products.append(ref)

        # Deduplicate, preserve order.
        result.subcategory_urls = list(dict.fromkeys(subcats))
        seen: set[str] = set()
        for p in products:
            if p.url not in seen:
                seen.add(p.url)
                result.product_refs.append(p)

        # Optional config-driven pagination (no-op on Safco).
        result.next_page_url = self._find_next_page(soup, page_url)
        return result

    # ------------------------------------------------------------------ #
    def _product_ref(self, item: dict) -> Optional[ProductRef]:
        # URL comes from `url` if present, else from `@id` minus the #fragment.
        url = item.get("url")
        if not url and item.get("@id"):
            url = item["@id"].split("#")[0]
        url = self._norm(url)
        if not url:
            return None

        offers = item.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        images = item.get("image") or []
        if isinstance(images, str):
            images = [images]

        price = offers.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        return ProductRef(
            url=url,
            name=item.get("name"),
            sku=item.get("sku"),
            price=price,
            currency=offers.get("priceCurrency"),
            image=images[0] if images else None,
        )

    # ------------------------------------------------------------------ #
    def _find_next_page(self, soup: BeautifulSoup, page_url: str) -> Optional[str]:
        """Return the next-page URL if the configured selector matches.

        Safco has no static pagination, so this returns None there. Kept so the
        navigator works unchanged on sites that paginate server-side.
        """
        sel = self.cfg.selectors.get("pagination_next")
        if not sel:
            return None
        el = soup.select_one(sel)
        if not el:
            return None
        href = el.get("href")
        return self._norm(urljoin(page_url, href)) if href else None

    # ------------------------------------------------------------------ #
    def _iter_jsonld(self, soup: BeautifulSoup):
        for tag in soup.find_all("script", type="application/ld+json"):
            raw = tag.string or tag.get_text()
            if not raw:
                continue
            yield from self._loads_jsonld(raw)

    @staticmethod
    def _loads_jsonld(raw: str) -> list[dict]:
        raw = raw.strip()
        out: list[dict] = []
        try:
            data = json.loads(raw)
            cands = data if isinstance(data, list) else [data]
            for c in cands:
                if isinstance(c, dict) and "@graph" in c:
                    out.extend(g for g in c["@graph"] if isinstance(g, dict))
                elif isinstance(c, dict):
                    out.append(c)
            return out
        except json.JSONDecodeError:
            for chunk in re.split(r"}\s*{", raw):
                chunk = chunk if chunk.startswith("{") else "{" + chunk
                chunk = chunk if chunk.endswith("}") else chunk + "}"
                try:
                    out.append(json.loads(chunk))
                except json.JSONDecodeError:
                    continue
        return out

    # ------------------------------------------------------------------ #
    def _norm(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        url = url.split("#")[0].split("?")[0].rstrip("/")
        return url or None

    def _in_scope(self, url: str) -> bool:
        host = urlparse(url).netloc
        return host in self._allowed


__all__ = ["Navigator", "NavigationResult", "ProductRef"]
