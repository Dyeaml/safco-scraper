"""
core/config.py
==============
Loads config.yaml into a validated, typed settings object.

Why a typed loader instead of passing a raw dict around:
  * fail fast — a malformed config errors at startup, not mid-crawl;
  * the rest of the codebase gets autocompletion and type safety;
  * secrets are resolved from the environment HERE, in one place, so no other
    module needs to know how secrets are sourced.

Secrets policy: config.yaml stores only the NAME of the env var holding a
secret (e.g. llm.api_key_env: "ANTHROPIC_API_KEY"). The actual value is read
from os.environ at load time and never written back to disk.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class RunCfg(BaseModel):
    project: str
    max_products: Optional[int] = None
    max_pages: Optional[int] = None


class SiteCfg(BaseModel):
    base_url: str
    allowed_domains: list[str]
    seed_categories: list[str]
    sitemap_url: Optional[str] = None


class PlaywrightCfg(BaseModel):
    headless: bool = True
    wait_until: str = "networkidle"
    wait_selector: Optional[str] = None


class FetcherCfg(BaseModel):
    type: Literal["httpx", "playwright"] = "httpx"
    timeout_seconds: float = 20.0
    headers: dict[str, str] = Field(default_factory=dict)
    playwright: PlaywrightCfg = Field(default_factory=PlaywrightCfg)


class RateLimitCfg(BaseModel):
    respect_robots_txt: bool = True
    max_concurrency: int = 4
    requests_per_second: float = 2.0
    delay_seconds: float = 0.5
    jitter_seconds: float = 0.3


class RetryCfg(BaseModel):
    max_attempts: int = 4
    backoff_base_seconds: float = 1.0
    backoff_factor: float = 2.0
    max_backoff_seconds: float = 30.0
    retry_on_status: list[int] = Field(default_factory=lambda: [429, 500, 502, 503, 504])


class ClassifierCfg(BaseModel):
    url_patterns: dict[str, str] = Field(default_factory=dict)
    product_jsonld_type: str = "Product"
    use_llm_fallback: bool = True


class ExtractionCfg(BaseModel):
    jsonld_types: dict[str, str] = Field(default_factory=dict)
    breadcrumb_drop: list[str] = Field(default_factory=list)
    selectors: dict[str, str] = Field(default_factory=dict)
    image_strip_query: bool = True
    use_llm_fallback: bool = True


class LLMCfg(BaseModel):
    enabled: bool = True
    provider: str = "anthropic"
    model: str = "claude-3-5-haiku-latest"
    api_key_env: str = "ANTHROPIC_API_KEY"
    max_tokens: int = 1500
    temperature: float = 0.0
    max_calls_per_run: int = 50

    @property
    def api_key(self) -> Optional[str]:
        """Resolve the secret from the environment. Never stored on the model."""
        return os.environ.get(self.api_key_env)

    @property
    def is_usable(self) -> bool:
        """LLM features only run if enabled AND a key is actually present."""
        return self.enabled and bool(self.api_key)


class ExportCfg(BaseModel):
    format: Literal["json", "csv", "xlsx"]
    path: str


class StorageCfg(BaseModel):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_path: str = "data/safco.db"
    upsert: bool = True
    exports: list[ExportCfg] = Field(default_factory=list)


class CheckpointCfg(BaseModel):
    enabled: bool = True
    retry_failed_on_resume: bool = True


class LoggingCfg(BaseModel):
    level: str = "INFO"
    format: Literal["json", "console"] = "json"
    file: Optional[str] = None
    emit_run_summary: bool = True


class Settings(BaseModel):
    """Root settings object — the whole validated config tree."""

    run: RunCfg
    site: SiteCfg
    fetcher: FetcherCfg = Field(default_factory=FetcherCfg)
    rate_limit: RateLimitCfg = Field(default_factory=RateLimitCfg)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    classifier: ClassifierCfg = Field(default_factory=ClassifierCfg)
    extraction: ExtractionCfg = Field(default_factory=ExtractionCfg)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    checkpoint: CheckpointCfg = Field(default_factory=CheckpointCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Read and validate config.yaml into a typed Settings object.

    Raises a clear error if the file is missing or malformed so the run fails
    fast at startup rather than deep inside the crawl.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {p.resolve()}")
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"Config root must be a mapping, got {type(raw).__name__}")
    return Settings(**raw)


__all__ = ["Settings", "load_settings"]
