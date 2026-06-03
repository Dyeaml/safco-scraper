"""
storage/db.py
=============
SQLite persistence for the prototype. Documented path to Postgres for prod
(same SQL shape; swap the connection + a few dialect details).

This module owns four concerns the spec calls out explicitly:

  * deduplication / idempotency
        Products are keyed by Product.dedup_key (sku, else url). Writes are
        UPSERTs. content_hash lets us skip no-op rewrites: re-scraping an
        unchanged product touches nothing and is reported as "unchanged".

  * resumability / checkpointing
        A url_queue table persists the frontier (pending/in_progress/done).
        On restart the orchestrator re-enqueues in_progress URLs and skips
        done ones, so a killed run resumes instead of starting over.

  * error handling / observability
        A failures table records every permanently-failed URL with its error.
        A run_summary table stores per-run metrics (pages, products, dedup
        hits, failures, llm_calls, duration).

Concurrency note: the scrape is async/single-process. SQLite is accessed from
one connection guarded by a lock, with WAL mode for resilience. For multi-
worker production you would move to Postgres; the schema is intentionally
portable (no SQLite-only column types in the core tables).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from core.models import Product


# --------------------------------------------------------------------------- #
# Queue item states for checkpointing
# --------------------------------------------------------------------------- #
class UrlState(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


@dataclass
class UpsertResult:
    """Outcome of persisting one product — drives observability counters."""
    dedup_key: str
    inserted: bool   # brand-new record
    updated: bool    # existed, content changed
    unchanged: bool  # existed, content_hash identical (idempotent no-op)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    dedup_key            TEXT PRIMARY KEY,           -- 'sku:PFWRL' or 'url:...'
    sku                  TEXT,
    product_name         TEXT NOT NULL,
    brand                TEXT,
    product_url          TEXT NOT NULL,
    description          TEXT,
    category_hierarchy   TEXT,                       -- JSON array of {name,url}
    price                REAL,
    currency             TEXT,
    availability         TEXT,
    pack_size            TEXT,
    variant_skus         TEXT,                       -- JSON array
    specifications       TEXT,                       -- JSON object
    image_urls           TEXT,                       -- JSON array
    alternative_products TEXT,                       -- JSON array
    source_url           TEXT,
    scraped_at           TEXT,
    extraction_method    TEXT,
    run_id               TEXT,
    content_hash         TEXT,
    first_seen_at        TEXT,                       -- set once, on insert
    last_updated_at      TEXT                        -- bumped on real change
);
CREATE INDEX IF NOT EXISTS idx_products_sku      ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_run      ON products(run_id);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_hierarchy);

CREATE TABLE IF NOT EXISTS url_queue (
    url          TEXT PRIMARY KEY,
    page_type    TEXT,                               -- classifier hint, nullable
    state        TEXT NOT NULL DEFAULT 'pending',
    depth        INTEGER DEFAULT 0,
    run_id       TEXT,
    enqueued_at  TEXT,
    updated_at   TEXT,
    attempts     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_state ON url_queue(state);

CREATE TABLE IF NOT EXISTS failures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    stage       TEXT,                                -- 'fetch'|'extract'|'validate'
    error       TEXT,
    run_id      TEXT,
    failed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_failures_run ON failures(run_id);

CREATE TABLE IF NOT EXISTS run_summary (
    run_id            TEXT PRIMARY KEY,
    started_at        TEXT,
    finished_at       TEXT,
    pages_fetched     INTEGER DEFAULT 0,
    products_inserted INTEGER DEFAULT 0,
    products_updated  INTEGER DEFAULT 0,
    products_unchanged INTEGER DEFAULT 0,
    failures          INTEGER DEFAULT 0,
    llm_calls         INTEGER DEFAULT 0,
    notes             TEXT
);
"""


def _json(value: Any) -> str:
    """Serialize lists/dicts/pydantic sub-objects to JSON for TEXT columns."""
    def default(o: Any):
        # pydantic sub-models (CategoryRef, ProductVariant) -> dict
        if hasattr(o, "model_dump"):
            return o.model_dump(mode="json")
        return str(o)
    return json.dumps(value, default=default, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class Store:
    """Async-friendly wrapper over a single SQLite connection.

    All public methods are async and serialize DB access through an asyncio
    lock, so the async crawl can call them freely without sqlite threading
    issues. The actual sqlite calls are synchronous but fast; for the prototype
    that's the right trade-off (simple, correct). Production -> Postgres + pool.
    """

    def __init__(self, sqlite_path: str):
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---- products: upsert with idempotency --------------------------------- #
    async def upsert_product(self, product: Product) -> UpsertResult:
        """Insert or update one product, keyed by dedup_key.

        Idempotency: if the row exists and content_hash is unchanged, nothing
        is written and `unchanged=True` is returned. This makes re-runs cheap
        and makes "what actually changed" observable.
        """
        if product.content_hash is None:
            product.finalize()  # ensure content_hash is set
        key = product.dedup_key
        now = datetime.now(timezone.utc).isoformat()

        async with self._lock:
            cur = self._conn.execute(
                "SELECT content_hash, first_seen_at FROM products WHERE dedup_key=?",
                (key,),
            )
            row = cur.fetchone()

            if row is not None and row["content_hash"] == product.content_hash:
                return UpsertResult(key, inserted=False, updated=False, unchanged=True)

            first_seen = row["first_seen_at"] if row else now
            values = {
                "dedup_key": key,
                "sku": product.sku,
                "product_name": product.product_name,
                "brand": product.brand,
                "product_url": str(product.product_url),
                "description": product.description,
                "category_hierarchy": _json([c.model_dump(mode="json") for c in product.category_hierarchy]),
                "price": product.price,
                "currency": product.currency,
                "availability": product.availability.value,
                "pack_size": product.pack_size,
                "variant_skus": _json([v.model_dump(mode="json") for v in product.variant_skus]),
                "specifications": _json(product.specifications),
                "image_urls": _json([str(u) for u in product.image_urls]),
                "alternative_products": _json([str(u) for u in product.alternative_products]),
                "source_url": str(product.source_url),
                "scraped_at": product.scraped_at.isoformat() if product.scraped_at else now,
                "extraction_method": product.extraction_method.value,
                "run_id": product.run_id,
                "content_hash": product.content_hash,
                "first_seen_at": first_seen,
                "last_updated_at": now,
            }
            cols = ", ".join(values.keys())
            placeholders = ", ".join(f":{k}" for k in values.keys())
            updates = ", ".join(f"{k}=excluded.{k}" for k in values if k != "dedup_key")
            self._conn.execute(
                f"INSERT INTO products ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT(dedup_key) DO UPDATE SET {updates}",
                values,
            )
            self._conn.commit()

            inserted = row is None
            return UpsertResult(key, inserted=inserted, updated=not inserted, unchanged=False)

    async def count_products(self) -> int:
        async with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]

    async def iter_products(self) -> list[dict]:
        """Return all products as dicts (JSON columns parsed) for export."""
        async with self._lock:
            rows = self._conn.execute("SELECT * FROM products").fetchall()
        out = []
        json_cols = {"category_hierarchy", "variant_skus", "specifications",
                     "image_urls", "alternative_products"}
        for r in rows:
            d = dict(r)
            for c in json_cols:
                try:
                    d[c] = json.loads(d[c]) if d[c] else []
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(d)
        return out

    # ---- url_queue: checkpointing / resumability --------------------------- #
    async def enqueue(self, url: str, page_type: Optional[str] = None,
                      depth: int = 0, run_id: Optional[str] = None) -> bool:
        """Add a URL to the frontier if not already known. Returns True if newly
        added, False if it was already seen (dedup of the crawl frontier)."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO url_queue (url, page_type, state, depth, run_id, enqueued_at, updated_at) "
                    "VALUES (?, ?, 'pending', ?, ?, ?, ?)",
                    (url, page_type, depth, run_id, now, now),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False  # already in queue/seen -> frontier dedup

    async def next_pending(self, limit: int = 1) -> list[dict]:
        """Atomically claim up to `limit` pending URLs (mark in_progress)."""
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM url_queue WHERE state='pending' ORDER BY depth, enqueued_at LIMIT ?",
                (limit,),
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE url_queue SET state='in_progress', attempts=attempts+1, updated_at=? WHERE url=?",
                    (now, r["url"]),
                )
            self._conn.commit()
            return [dict(r) for r in rows]

    async def mark(self, url: str, state: UrlState) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._conn.execute(
                "UPDATE url_queue SET state=?, updated_at=? WHERE url=?",
                (state.value, now, url),
            )
            self._conn.commit()

    async def reset_in_progress(self) -> int:
        """On resume, put any in_progress URLs (from a killed run) back to
        pending so they get retried. Returns how many were reset."""
        async with self._lock:
            cur = self._conn.execute(
                "UPDATE url_queue SET state='pending' WHERE state='in_progress'"
            )
            self._conn.commit()
            return cur.rowcount

    async def queue_counts(self) -> dict[str, int]:
        async with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS c FROM url_queue GROUP BY state"
            ).fetchall()
        return {r["state"]: r["c"] for r in rows}

    # ---- failures: error handling ------------------------------------------ #
    async def record_failure(self, url: str, stage: str, error: str,
                             run_id: Optional[str] = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._conn.execute(
                "INSERT INTO failures (url, stage, error, run_id, failed_at) VALUES (?, ?, ?, ?, ?)",
                (url, stage, error, run_id, now),
            )
            self._conn.commit()

    # ---- run_summary: observability ---------------------------------------- #
    async def start_run(self, run_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO run_summary (run_id, started_at) VALUES (?, ?)",
                (run_id, now),
            )
            self._conn.commit()

    async def finish_run(self, run_id: str, **metrics: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        allowed = {"pages_fetched", "products_inserted", "products_updated",
                   "products_unchanged", "failures", "llm_calls", "notes"}
        sets = ["finished_at=?"]
        params: list[Any] = [now]
        for k, v in metrics.items():
            if k in allowed:
                sets.append(f"{k}=?")
                params.append(v)
        params.append(run_id)
        async with self._lock:
            self._conn.execute(
                f"UPDATE run_summary SET {', '.join(sets)} WHERE run_id=?", params
            )
            self._conn.commit()

    async def get_run_summary(self, run_id: str) -> Optional[dict]:
        async with self._lock:
            row = self._conn.execute(
                "SELECT * FROM run_summary WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    async def aclose(self) -> None:
        async with self._lock:
            self._conn.close()


__all__ = ["Store", "UrlState", "UpsertResult"]
