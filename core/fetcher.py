"""
core/fetcher.py
===============
The async fetch layer. Everything that touches the network goes through here,
so this is the single place that enforces politeness and resilience:

  * rate limiting   — global token bucket (requests/sec) + concurrency semaphore
  * retries         — exponential backoff with jitter, transient errors only
  * error handling   — typed FetchResult; one bad page never crashes the run
  * pluggability     — Fetcher is an interface; HttpxFetcher is the SSR impl,
                       PlaywrightFetcher is a documented drop-in for JS pages.

The agents upstream (navigator, extractor) depend only on the `Fetcher`
interface and the `FetchResult` dataclass — never on httpx directly. Swapping
the fetcher `type` in config.yaml changes the implementation without touching
any downstream code.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol
from urllib.parse import urlparse

import httpx

from .config import FetcherCfg, RateLimitCfg, RetryCfg


# --------------------------------------------------------------------------- #
# Result type — fetchers never raise for HTTP/transport errors; they return a
# FetchResult. This keeps the orchestrator's control flow simple: it inspects
# .ok and .should_retry rather than wrapping every call in try/except.
# --------------------------------------------------------------------------- #
@dataclass
class FetchResult:
    url: str
    status: Optional[int] = None      # HTTP status, or None on transport error
    text: str = ""
    error: Optional[str] = None        # populated on failure
    elapsed_seconds: float = 0.0
    attempts: int = 0
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300


# --------------------------------------------------------------------------- #
# Token-bucket rate limiter — a global ceiling on requests/sec, independent of
# concurrency. Concurrency caps *simultaneous* requests; the bucket caps the
# *rate*. Together they keep us a good citizen even with many workers.
# --------------------------------------------------------------------------- #
class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: Optional[float] = None):
        self.rate = max(rate_per_second, 0.001)
        self.capacity = capacity if capacity is not None else max(rate_per_second, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            async with self._lock:
                now = time.monotonic()
                # Refill proportional to elapsed time, capped at capacity.
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # How long until the next token?
                deficit = 1.0 - self._tokens
                wait = deficit / self.rate
            await asyncio.sleep(wait)


# --------------------------------------------------------------------------- #
# Fetcher interface — the contract upstream agents depend on.
# --------------------------------------------------------------------------- #
class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
    async def aclose(self) -> None: ...


# --------------------------------------------------------------------------- #
# Shared resilience policy — rate limiting + retry/backoff. Both concrete
# fetchers reuse this so the politeness/retry logic lives in exactly one place.
# --------------------------------------------------------------------------- #
class ResiliencePolicy:
    def __init__(self, rate_cfg: RateLimitCfg, retry_cfg: RetryCfg):
        self.rate_cfg = rate_cfg
        self.retry_cfg = retry_cfg
        self._bucket = TokenBucket(rate_cfg.requests_per_second)
        self._semaphore = asyncio.Semaphore(rate_cfg.max_concurrency)
        # Track the last request time per host to enforce delay_seconds.
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    def _host_lock(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def _throttle(self, url: str) -> None:
        """Enforce per-host min delay + jitter, then the global token bucket."""
        host = urlparse(url).netloc
        async with self._host_lock(host):
            now = time.monotonic()
            last = self._last_request.get(host, 0.0)
            min_gap = self.rate_cfg.delay_seconds + random.uniform(0, self.rate_cfg.jitter_seconds)
            wait = (last + min_gap) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = time.monotonic()
        await self._bucket.acquire()

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter for retry `attempt` (1-based)."""
        raw = self.retry_cfg.backoff_base_seconds * (self.retry_cfg.backoff_factor ** (attempt - 1))
        capped = min(raw, self.retry_cfg.max_backoff_seconds)
        return random.uniform(0, capped)  # full jitter avoids thundering herd

    def should_retry_status(self, status: int) -> bool:
        return status in self.retry_cfg.retry_on_status

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    @property
    def max_attempts(self) -> int:
        return self.retry_cfg.max_attempts


# --------------------------------------------------------------------------- #
# HttpxFetcher — the implementation for server-rendered pages (Safco is SSR).
# --------------------------------------------------------------------------- #
class HttpxFetcher:
    def __init__(self, fetcher_cfg: FetcherCfg, policy: ResiliencePolicy):
        self.cfg = fetcher_cfg
        self.policy = policy
        self._client = httpx.AsyncClient(
            timeout=fetcher_cfg.timeout_seconds,
            headers=fetcher_cfg.headers,
            follow_redirects=True,
        )

    async def fetch(self, url: str) -> FetchResult:
        """Fetch one URL with rate limiting + retry/backoff.

        Returns a FetchResult in all cases — never raises for HTTP/transport
        failures. The caller decides what to do with a non-ok result (log,
        record to the failures table, etc.).
        """
        start = time.monotonic()
        last_error: Optional[str] = None
        last_status: Optional[int] = None

        for attempt in range(1, self.policy.max_attempts + 1):
            await self.policy._throttle(url)
            async with self.policy.semaphore:
                try:
                    resp = await self._client.get(url)
                    last_status = resp.status_code

                    if 200 <= resp.status_code < 300:
                        return FetchResult(
                            url=url, status=resp.status_code, text=resp.text,
                            elapsed_seconds=time.monotonic() - start, attempts=attempt,
                        )

                    # Retry transient statuses; give up immediately on others (e.g. 404).
                    if self.policy.should_retry_status(resp.status_code) and attempt < self.policy.max_attempts:
                        last_error = f"HTTP {resp.status_code}"
                        await asyncio.sleep(self.policy._backoff_delay(attempt))
                        continue

                    return FetchResult(
                        url=url, status=resp.status_code,
                        error=f"HTTP {resp.status_code}",
                        elapsed_seconds=time.monotonic() - start, attempts=attempt,
                    )

                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    # Transport errors are always transient — retry with backoff.
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self.policy.max_attempts:
                        await asyncio.sleep(self.policy._backoff_delay(attempt))
                        continue

        return FetchResult(
            url=url, status=last_status, error=last_error or "unknown error",
            elapsed_seconds=time.monotonic() - start, attempts=self.policy.max_attempts,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------------------- #
# PlaywrightFetcher — documented drop-in for JS-rendered pages. Not needed for
# Safco (server-rendered), so left as a guarded stub: it satisfies the Fetcher
# interface and explains exactly what a production impl would do. Selecting
# fetcher.type="playwright" without the dependency fails fast with guidance.
# --------------------------------------------------------------------------- #
class PlaywrightFetcher:
    def __init__(self, fetcher_cfg: FetcherCfg, policy: ResiliencePolicy):
        self.cfg = fetcher_cfg
        self.policy = policy
        self._browser = None
        try:
            import playwright  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "fetcher.type='playwright' requires the playwright package "
                "(`pip install playwright && playwright install chromium`). "
                "Safco Dental is server-rendered, so 'httpx' is the right choice here; "
                "Playwright is only needed for JS-rendered targets."
            ) from exc

    async def fetch(self, url: str) -> FetchResult:  # pragma: no cover - documented path
        # Production shape: reuse a single browser, throttle via self.policy,
        # navigate with wait_until=self.cfg.playwright.wait_until, optionally
        # wait for wait_selector, then return page.content() as .text. The
        # retry/backoff and rate-limit logic is identical to HttpxFetcher and
        # would be factored out of both rather than duplicated.
        raise NotImplementedError("PlaywrightFetcher stub — not needed for Safco (SSR).")

    async def aclose(self) -> None:
        if self._browser is not None:
            await self._browser.close()


# --------------------------------------------------------------------------- #
# Factory — picks the implementation from config. The one place that knows
# about concrete fetcher classes.
# --------------------------------------------------------------------------- #
def build_fetcher(fetcher_cfg: FetcherCfg, rate_cfg: RateLimitCfg, retry_cfg: RetryCfg) -> Fetcher:
    policy = ResiliencePolicy(rate_cfg, retry_cfg)
    if fetcher_cfg.type == "httpx":
        return HttpxFetcher(fetcher_cfg, policy)
    if fetcher_cfg.type == "playwright":
        return PlaywrightFetcher(fetcher_cfg, policy)
    raise ValueError(f"Unknown fetcher type: {fetcher_cfg.type!r}")


__all__ = [
    "FetchResult", "Fetcher", "TokenBucket", "ResiliencePolicy",
    "HttpxFetcher", "PlaywrightFetcher", "build_fetcher",
]
