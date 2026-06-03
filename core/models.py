"""
core/models.py
==============
The data contract shared by every agent in the pipeline.

Design notes (these map directly to decisions made during site analysis):

* Safco Dental is server-rendered Magento. Each product page embeds two
  JSON-LD blocks: a `Product` block and a `BreadcrumbList` block. Those are the
  PRIMARY extraction source. DOM selectors and an LLM fallback fill the gaps
  (specs, alternatives, pack-size variants).

* SKU model: the JSON-LD `sku` (e.g. "PFWRL") is the canonical PARENT product
  code and is always present, so it is our primary dedup key. A product may
  also expose purchasable VARIANT skus (e.g. "RFP10X20", "RFP6X25") for
  different pack sizes; those live in the DOM and are captured opportunistically
  in `variant_skus`. We do NOT block extraction on variants — full per-variant
  price/pack-size is a documented extension point.

* Provenance fields (`source_url`, `scraped_at`, `extraction_method`, `run_id`,
  `content_hash`) make every record auditable and support idempotency: re-running
  a URL upserts by `dedup_key`, and `content_hash` detects real changes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


# --------------------------------------------------------------------------- #
# Enums / controlled vocabularies
# --------------------------------------------------------------------------- #
class Availability(str, Enum):
    """Normalized stock status. Raw schema.org values map onto these."""

    IN_STOCK = "InStock"
    OUT_OF_STOCK = "OutOfStock"
    PREORDER = "PreOrder"
    BACKORDER = "BackOrder"
    DISCONTINUED = "Discontinued"
    UNKNOWN = "Unknown"

    @classmethod
    def from_schema_org(cls, raw: Optional[str]) -> "Availability":
        """Map a schema.org availability URL/string onto our enum.

        Accepts forms like 'https://schema.org/InStock', 'schema.org/InStock',
        or bare 'InStock'. Unknown / missing values fall back to UNKNOWN so the
        pipeline never crashes on an unexpected vocabulary.
        """
        if not raw:
            return cls.UNKNOWN
        token = raw.rstrip("/").split("/")[-1].strip()
        mapping = {
            "InStock": cls.IN_STOCK,
            "OutOfStock": cls.OUT_OF_STOCK,
            "PreOrder": cls.PREORDER,
            "BackOrder": cls.BACKORDER,
            "Discontinued": cls.DISCONTINUED,
        }
        return mapping.get(token, cls.UNKNOWN)


class ExtractionMethod(str, Enum):
    """Provenance: how this record's fields were obtained.

    Recorded per-record so the validator/observability layer can report what
    fraction of products needed the (slower, costlier, non-deterministic) LLM
    fallback versus deterministic JSON-LD / DOM extraction.
    """

    JSON_LD = "json_ld"           # primary: parsed from embedded JSON-LD
    DOM_SELECTOR = "dom_selector"  # secondary: CSS/XPath selectors
    LLM_FALLBACK = "llm_fallback"  # fallback: LLM read irregular HTML
    MIXED = "mixed"               # combination of the above


class PageType(str, Enum):
    """Classifier output — what a fetched page is."""

    CATEGORY = "category"          # top-level catalog category
    SUBCATEGORY = "subcategory"    # nested category
    LISTING = "listing"            # product listing / grid (may be paginated)
    PRODUCT = "product"            # product detail page
    OTHER = "other"                # nav junk, footer, unknown


# --------------------------------------------------------------------------- #
# Sub-models
# --------------------------------------------------------------------------- #
class ProductVariant(BaseModel):
    """A purchasable pack-size / option variant of a parent product.

    Populated opportunistically from the DOM. `sku` is required; price and
    pack_size are best-effort.
    """

    sku: str = Field(..., description="Variant-level item number, e.g. 'RFP10X20'")
    pack_size: Optional[str] = Field(
        None, description="Human-readable pack/unit size, e.g. '10mm x 20mm'"
    )
    price: Optional[float] = Field(None, ge=0, description="Variant price if exposed")

    @field_validator("sku")
    @classmethod
    def _strip_sku(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("variant sku must be non-empty")
        return v


class CategoryRef(BaseModel):
    """A single node in the category hierarchy, with its URL when known.

    Derived from the JSON-LD BreadcrumbList. Keeping the URL alongside the name
    lets the navigator reconstruct traversal paths and lets queries filter by
    canonical category URL rather than fragile display names.
    """

    name: str
    url: Optional[HttpUrl] = None


# --------------------------------------------------------------------------- #
# Main record
# --------------------------------------------------------------------------- #
class Product(BaseModel):
    """One scraped product. This is the canonical row written to storage and
    exported to CSV/JSON.

    Required fields are the minimum we accept as a valid record: a name, a
    canonical URL, and a dedup key. Everything else is nullable because the
    spec explicitly says "capture as many of the following as possible" — a
    product legitimately may not expose a price, pack size, or specs.
    """

    # --- Core identity ---------------------------------------------------- #
    product_name: str = Field(..., min_length=1)
    sku: Optional[str] = Field(
        None,
        description="Canonical parent product code from JSON-LD, e.g. 'PFWRL'. "
        "Primary dedup key when present.",
    )
    product_url: HttpUrl = Field(..., description="Canonical product detail URL")

    # --- Descriptive ------------------------------------------------------ #
    brand: Optional[str] = Field(None, description="Brand / manufacturer")
    description: Optional[str] = Field(None, description="Cleaned plain-text description")
    category_hierarchy: list[CategoryRef] = Field(
        default_factory=list,
        description="Ordered category path, root→leaf, excluding Home and the "
        "product itself.",
    )

    # --- Commercial ------------------------------------------------------- #
    price: Optional[float] = Field(None, ge=0, description="Parent/displayed price")
    currency: Optional[str] = Field(None, description="ISO currency code, e.g. 'USD'")
    availability: Availability = Field(default=Availability.UNKNOWN)
    pack_size: Optional[str] = Field(
        None, description="Parent-level unit/pack size if shown outside variants"
    )
    variant_skus: list[ProductVariant] = Field(
        default_factory=list,
        description="Purchasable pack-size variants captured from the DOM.",
    )

    # --- Rich attributes -------------------------------------------------- #
    specifications: dict[str, str] = Field(
        default_factory=dict,
        description="Free-form spec/attribute key→value pairs from the DOM.",
    )
    image_urls: list[HttpUrl] = Field(default_factory=list)
    alternative_products: list[HttpUrl] = Field(
        default_factory=list,
        description="URLs of related / 'you may also like' products.",
    )

    # --- Provenance / operational ---------------------------------------- #
    source_url: HttpUrl = Field(
        ..., description="The exact URL fetched (may differ from canonical url)."
    )
    scraped_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp of extraction.",
    )
    extraction_method: ExtractionMethod = Field(default=ExtractionMethod.JSON_LD)
    run_id: Optional[str] = Field(
        None, description="Identifier of the scrape run that produced this record."
    )
    content_hash: Optional[str] = Field(
        None,
        description="Hash of the meaningful content fields; used to detect real "
        "changes between runs (idempotency).",
    )

    # ------------------------------------------------------------------ #
    # Validators / normalizers
    # ------------------------------------------------------------------ #
    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: Optional[str]) -> Optional[str]:
        return v.upper().strip() if v else v

    @field_validator("brand", "description", "pack_size")
    @classmethod
    def _strip_text(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None  # collapse empty strings to None

    @model_validator(mode="after")
    def _ensure_dedup_key(self) -> "Product":
        """A record must be uniquely addressable. Prefer SKU; fall back to the
        canonical URL. This guarantees `dedup_key` is never empty so storage's
        unique constraint always has something to anchor on.
        """
        if not self.sku and not self.product_url:
            raise ValueError("Product needs at least one of: sku, product_url")
        return self

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def dedup_key(self) -> str:
        """Stable primary key for upsert / deduplication.

        SKU is preferred (stable across URL changes); URL is the fallback when a
        product page omits a SKU. Prefixed so the two key spaces never collide.
        """
        if self.sku:
            return f"sku:{self.sku.strip().upper()}"
        return f"url:{str(self.product_url).rstrip('/').lower()}"

    def compute_content_hash(self) -> str:
        """Deterministic hash of the meaningful content fields.

        Excludes volatile/operational fields (scraped_at, run_id, source_url)
        so that re-scraping an unchanged product yields the same hash → the
        storage layer can skip a no-op write and observability can count real
        changes vs. re-visits.
        """
        parts = [
            self.product_name,
            self.sku or "",
            self.brand or "",
            self.description or "",
            f"{self.price}",
            self.currency or "",
            self.availability.value,
            self.pack_size or "",
            "|".join(sorted(v.sku for v in self.variant_skus)),
            "|".join(f"{k}={self.specifications[k]}" for k in sorted(self.specifications)),
            "|".join(sorted(str(u) for u in self.image_urls)),
            "|".join(c.name for c in self.category_hierarchy),
        ]
        blob = "\x1f".join(parts)  # unit-separator: avoids accidental collisions
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def finalize(self, run_id: Optional[str] = None) -> "Product":
        """Stamp run_id and content_hash just before persistence."""
        if run_id is not None:
            self.run_id = run_id
        self.content_hash = self.compute_content_hash()
        return self


__all__ = [
    "Availability",
    "ExtractionMethod",
    "PageType",
    "ProductVariant",
    "CategoryRef",
    "Product",
]
