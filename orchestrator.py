"""
orchestrator.py
===============
The control plane. Owns the run lifecycle and drives the pipeline:

    seed queue
      -> claim pending URL (checkpointed)
        -> fetch (rate-limited, retried)
          -> classify (rules; LLM tie-break optional)
            -> LISTING/CATEGORY: navigate -> enqueue subcats + product URLs
            -> PRODUCT:          extract -> validate -> upsert (dedup/idempotent)
        -> mark done / record failure
    -> until queue empty or run limits hit
    -> export + run summary

This is deliberately NOT an LLM agent — it is deterministic coordination. The
"agentic" intelligence lives in the specialized agents it calls (navigator,
classifier, extractor) and in the selective LLM fallbacks inside them. That
separation is the whole point of the design: each agent has one job, the
orchestrator sequences them, and the work queue + checkpoint tables make the
whole thing resumable and observable.

Concurrency: products are fetched+extracted concurrently up to the configured
limit via an asyncio worker pool, while the rate limiter keeps us polite. The
queue lives in SQLite so a killed run resumes instead of restarting.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from agents.classifier import Classifier
from agents.extractor import Extractor, ExtractionError
from agents.navigator import Navigator, ProductRef
from core.config import Settings
from core.fetcher import Fetcher, build_fetcher
from core.logging_config import setup_logging
from core.models import (
    Availability,
    CategoryRef,
    ExtractionMethod,
    PageType,
    Product,
)
from storage.db import Store, UrlState
from storage.export import run_exports
from llm.client import build_llm


@dataclass
class Metrics:
    pages_fetched: int = 0
    products_inserted: int = 0
    products_updated: int = 0
    products_unchanged: int = 0
    failures: int = 0
    llm_calls: int = 0

    def as_dict(self) -> dict:
        return {
            "pages_fetched": self.pages_fetched,
            "products_inserted": self.products_inserted,
            "products_updated": self.products_updated,
            "products_unchanged": self.products_unchanged,
            "failures": self.failures,
            "llm_calls": self.llm_calls,
        }


class Orchestrator:
    def __init__(self, settings: Settings,
                 fetcher: Optional[Fetcher] = None,
                 store: Optional[Store] = None,
                 navigator: Optional[Navigator] = None,
                 classifier: Optional[Classifier] = None,
                 extractor: Optional[Extractor] = None):
        self.s = settings
        self.log = setup_logging(settings.logging.level, settings.logging.format,
                                 settings.logging.file)
        # Dependency injection: real components by default, but tests/fixtures
        # can pass fakes (e.g. a fetcher that reads local HTML).
        self.fetcher = fetcher or build_fetcher(settings.fetcher, settings.rate_limit, settings.retry)
        self.store = store or Store(settings.storage.sqlite_path)
        # Build the LLM client once (or None if disabled / no key). The same
        # client backs both the classifier tie-break and the extractor fallback,
        # so the per-run call cap is shared across both uses.
        self.llm = build_llm(settings.llm)
        if self.llm is not None:
            self.log.info("llm fallback enabled", extra={"model": settings.llm.model})
        self.navigator = navigator or Navigator(settings.site, settings.extraction)
        self.classifier = classifier or Classifier(settings.classifier, llm_classifier=self.llm)
        self.extractor = extractor or Extractor(settings.extraction, llm_extractor=self.llm)
        self.metrics = Metrics()
        self.run_id = f"{settings.run.project}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"

    # ------------------------------------------------------------------ #
    async def run(self) -> Metrics:
        self.log.info("run starting", extra={"run_id": self.run_id})
        await self.store.start_run(self.run_id)

        # Resume support: re-queue anything a prior killed run left in-progress.
        if self.s.checkpoint.enabled:
            reset = await self.store.reset_in_progress()
            if reset:
                self.log.info("resumed run", extra={"run_id": self.run_id, "requeued": reset})

        # Seed the frontier with the configured category URLs.
        for url in self.s.site.seed_categories:
            await self.store.enqueue(url, page_type="category", depth=0, run_id=self.run_id)

        await self._drain_queue()

        # Exports + run summary.
        exported = await run_exports(self.store, self.s.storage.exports)
        await self.store.finish_run(self.run_id, **self.metrics.as_dict())
        self.log.info("run finished",
                      extra={"run_id": self.run_id, **self.metrics.as_dict(),
                             "exports": exported})
        return self.metrics

    # ------------------------------------------------------------------ #
    async def _drain_queue(self) -> None:
        """Process the queue with bounded concurrency until empty or limited."""
        concurrency = self.s.rate_limit.max_concurrency
        while not self._limit_reached():
            batch = await self.store.next_pending(limit=concurrency)
            if not batch:
                break  # queue empty
            await asyncio.gather(*(self._process(item) for item in batch))

    def _limit_reached(self) -> bool:
        mp = self.s.run.max_products
        pg = self.s.run.max_pages
        if mp is not None and (self.metrics.products_inserted + self.metrics.products_updated) >= mp:
            return True
        if pg is not None and self.metrics.pages_fetched >= pg:
            return True
        return False

    # ------------------------------------------------------------------ #
    async def _process(self, item: dict) -> None:
        """Fetch one URL, classify it, then navigate or extract."""
        url = item["url"]
        try:
            result = await self.fetcher.fetch(url)
            self.metrics.pages_fetched += 1
            if not result.ok:
                self.log.warning("fetch failed",
                                 extra={"run_id": self.run_id, "url": url,
                                        "status": result.status, "error": result.error})
                await self.store.record_failure(url, "fetch", result.error or "fetch error", self.run_id)
                await self.store.mark(url, UrlState.FAILED)
                self.metrics.failures += 1
                return

            classification = self.classifier.classify(url, result.text)
            if classification.used_llm:
                self.metrics.llm_calls += 1

            if classification.page_type in (PageType.CATEGORY, PageType.SUBCATEGORY, PageType.LISTING):
                await self._handle_listing(url, result.text, item.get("depth", 0))
            elif classification.page_type == PageType.PRODUCT:
                await self._handle_product(url, result.text)
            else:
                self.log.info("skipped page",
                              extra={"run_id": self.run_id, "url": url,
                                     "page_type": classification.page_type.value})

            await self.store.mark(url, UrlState.DONE)

        except Exception as exc:  # never let one page crash the run
            self.log.error("processing error", extra={"run_id": self.run_id, "url": url}, exc_info=True)
            await self.store.record_failure(url, "process", f"{type(exc).__name__}: {exc}", self.run_id)
            await self.store.mark(url, UrlState.FAILED)
            self.metrics.failures += 1

    # ------------------------------------------------------------------ #
    async def _handle_listing(self, url: str, html: str, depth: int) -> None:
        nav = self.navigator.discover(html, url)
        # Enqueue subcategories (recurse).
        for sub in nav.subcategory_urls:
            await self.store.enqueue(sub, page_type="subcategory", depth=depth + 1, run_id=self.run_id)
        # Enqueue product detail URLs; stash the listing's partial data as a
        # fallback record so a later detail-fetch failure doesn't lose the product.
        for ref in nav.product_refs:
            newly = await self.store.enqueue(ref.url, page_type="product", depth=depth + 1, run_id=self.run_id)
            if newly:
                await self._store_listing_fallback(ref, url)
        # Server-side pagination, if any (no-op on Safco).
        if nav.next_page_url:
            await self.store.enqueue(nav.next_page_url, page_type="listing", depth=depth, run_id=self.run_id)
        self.log.info("listing processed",
                      extra={"run_id": self.run_id, "url": url,
                             "subcats": len(nav.subcategory_urls),
                             "products": len(nav.product_refs)})

    async def _store_listing_fallback(self, ref: ProductRef, listing_url: str) -> None:
        """Persist the thin record available from the listing. The richer
        detail-page extraction will UPSERT over this later (content_hash makes
        the upgrade a real update, not a duplicate)."""
        if not ref.name:
            return
        try:
            p = Product(
                product_name=ref.name,
                sku=ref.sku,
                product_url=ref.url,
                price=ref.price,
                currency=ref.currency,
                availability=Availability.UNKNOWN,
                image_urls=[ref.image] if ref.image else [],
                category_hierarchy=[],
                source_url=listing_url,
                extraction_method=ExtractionMethod.JSON_LD,
            ).finalize(run_id=self.run_id)
            res = await self.store.upsert_product(p)
            self._count_upsert(res)
        except Exception:
            pass  # fallback is best-effort; detail extraction is the real source

    # ------------------------------------------------------------------ #
    async def _handle_product(self, url: str, html: str) -> None:
        try:
            product = self.extractor.extract(html, source_url=url)
            if product.extraction_method == ExtractionMethod.LLM_FALLBACK:
                self.metrics.llm_calls += 1
            product.finalize(run_id=self.run_id)
            res = await self.store.upsert_product(product)
            self._count_upsert(res)
            self.log.info("product stored",
                          extra={"run_id": self.run_id, "url": url, "sku": product.sku,
                                 "method": product.extraction_method.value,
                                 "outcome": "inserted" if res.inserted else "updated" if res.updated else "unchanged"})
        except ExtractionError as exc:
            self.log.warning("extraction failed", extra={"run_id": self.run_id, "url": url, "error": str(exc)})
            await self.store.record_failure(url, "extract", str(exc), self.run_id)
            self.metrics.failures += 1

    def _count_upsert(self, res) -> None:
        if res.inserted:
            self.metrics.products_inserted += 1
        elif res.updated:
            self.metrics.products_updated += 1
        elif res.unchanged:
            self.metrics.products_unchanged += 1

    # ------------------------------------------------------------------ #
    async def aclose(self) -> None:
        await self.fetcher.aclose()
        await self.store.aclose()


__all__ = ["Orchestrator", "Metrics"]
