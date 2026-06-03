"""
main.py
=======
CLI entrypoint. Loads config, runs the orchestrator, prints the run summary.

Usage:
    python main.py                      # uses config.yaml
    python main.py --config other.yaml  # different target/site
    python main.py --max-products 50    # override a limit for a quick run

Secrets (LLM key) come from the environment / .env, never the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

from core.config import load_settings
from orchestrator import Orchestrator


def _load_dotenv() -> None:
    """Minimal .env loader (no dependency). Real deploys use the platform's
    secret injection; this is a dev convenience."""
    env = Path(".env")
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


async def _run(config_path: str, overrides: dict) -> None:
    settings = load_settings(config_path)
    # Apply CLI overrides.
    if overrides.get("max_products") is not None:
        settings.run.max_products = overrides["max_products"]
    if overrides.get("max_pages") is not None:
        settings.run.max_pages = overrides["max_pages"]

    orch = Orchestrator(settings)
    try:
        metrics = await orch.run()
        summary = await orch.store.get_run_summary(orch.run_id)
    finally:
        await orch.aclose()

    print("\n=== RUN SUMMARY ===")
    print(f"run_id            : {orch.run_id}")
    for k, v in metrics.as_dict().items():
        print(f"{k:18}: {v}")
    if summary:
        print(f"started_at        : {summary.get('started_at')}")
        print(f"finished_at       : {summary.get('finished_at')}")


def main() -> None:
    _load_dotenv()
    ap = argparse.ArgumentParser(description="Agentic product scraper")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--max-products", type=int, default=None)
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()
    asyncio.run(_run(args.config, {
        "max_products": args.max_products,
        "max_pages": args.max_pages,
    }))


if __name__ == "__main__":
    main()
