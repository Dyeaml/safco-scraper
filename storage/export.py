"""
storage/export.py
================
Exports the stored products to structured files (JSON / CSV). The DB is the
source of truth; exports are derived artifacts for querying/sharing.

Schema is documented in the README; the JSON export preserves nested
structures (category hierarchy, variants, specs, image lists) while the CSV
flattens them (JSON-encoded cells for the nested fields) so it opens cleanly
in Excel/Sheets.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from storage.db import Store


# CSV column order — flat, human-friendly. Nested fields are JSON-encoded.
_CSV_COLUMNS = [
    "dedup_key", "sku", "product_name", "brand", "product_url",
    "price", "currency", "availability", "pack_size",
    "category_hierarchy", "variant_skus", "specifications",
    "image_urls", "alternative_products", "description",
    "source_url", "scraped_at", "extraction_method", "run_id",
    "content_hash", "first_seen_at", "last_updated_at",
]


async def export_json(store: Store, path: str) -> int:
    """Write all products to a JSON array. Returns the count written."""
    rows = await store.iter_products()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2, default=str)
    return len(rows)


async def export_csv(store: Store, path: str) -> int:
    """Write all products to a CSV. Nested fields are JSON-encoded into cells.
    Returns the count written."""
    rows = await store.iter_products()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    nested = {"category_hierarchy", "variant_skus", "specifications",
              "image_urls", "alternative_products"}
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = dict(r)
            for col in nested:
                if col in row and not isinstance(row[col], str):
                    row[col] = json.dumps(row[col], ensure_ascii=False, default=str)
            writer.writerow(row)
    return len(rows)


async def run_exports(store: Store, exports: list) -> dict[str, int]:
    """Run all configured exports. `exports` is the list of ExportCfg."""
    results: dict[str, int] = {}
    for exp in exports:
        if exp.format == "json":
            results[exp.path] = await export_json(store, exp.path)
        elif exp.format == "csv":
            results[exp.path] = await export_csv(store, exp.path)
        # xlsx could be added with the xlsx skill; JSON/CSV satisfy the spec.
    return results


__all__ = ["export_json", "export_csv", "run_exports"]
