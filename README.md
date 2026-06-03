# Agentic Product Scraper — Safco Dental

A working prototype of an agent-based scraping system that discovers product
categories on a target site, traverses them, extracts and normalizes product
data, stores it in a queryable format, and is structured so it can be hardened
for production.

Target site for this exercise: **safcodental.com**, scoped to two categories —
`sutures-surgical-products` and `gloves`.

---

## 1. Quick start

```bash
# 1. create + activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS/Linux/Git-Bash
# .\.venv\Scripts\Activate.ps1     # Windows PowerShell

# 2. install dependencies
pip install -r requirements.txt

# 3. (optional) enable the LLM fallback — only needed for irregular pages
cp .env.example .env               # then put the LLM API Key in .env

# 4. run
python main.py                     # uses config.yaml(default max is 200)
python main.py --max-products 20   # quick bounded run(run only for 20 products)
```

Outputs land in `data/`:

| File | What it is |
|------|------------|
| `data/exports/products.json` | structured JSON of all products |
| `data/exports/products.csv`  | spreadsheet-friendly export |
| `data/safco.db`              | SQLite database (source of truth) |
| `data/logs/run.log`          | structured JSON run log |

A human-readable DB dump: `sqlite3 data/safco.db .dump > data/safco_dump.sql`.

---

## 2. From ambiguous request to design

The brief was "scrape the products." Inspecting the site turned that into
concrete engineering decisions:

* **The site is server-rendered Magento.** A naive request returns a
  "JavaScript disabled" shell, but a request with a realistic `User-Agent`
  receives full HTML. → No headless browser needed; an async HTTP client is
  enough. The fetcher is still behind a pluggable interface so a JS-rendered
  target could drop in a Playwright implementation without touching anything
  else.
* **Products are exposed as JSON-LD.** Every product page embeds a `Product`
  and a `BreadcrumbList` block; every listing page embeds two `ItemList`
  blocks (one of subcategories, one of products). → JSON-LD is the **primary**
  extraction and navigation source: clean, structured, stable. DOM selectors
  and an LLM fallback handle only what JSON-LD omits.
* **SKUs are layered.** The JSON-LD `sku` (e.g. `PFWRL`) is the canonical
  *parent* code; pack-size *variant* SKUs (e.g. `RFP10X20`) live in embedded
  config. → The parent SKU is the dedup key; variants are captured
  opportunistically and full per-variant extraction is a documented extension.

This is why the LLM is a *fallback*, not the engine: the deterministic path
handles ~100% of this site (a full run logged **0 LLM calls**), and the LLM is
reserved for pages that break the JSON-LD pattern.

---

## 3. Architecture — agentic, not monolithic

```
            ┌────────────────────────── Orchestrator ──────────────────────────┐
            │  (control plane: queue, checkpointing, limits, run summary)       │
            └───────┬───────────────────────────────────────────────────────────┘
                    │ claim URL from SQLite queue
                    ▼
              ┌──────────┐   rate-limited, retried
              │ Fetcher  │   (httpx async; Playwright drop-in)
              └────┬─────┘
                   ▼
            ┌──────────────┐  rules first, LLM tie-break only if ambiguous
            │  Classifier  │
            └──────┬───────┘
          listing/ │ product
          category │
          ┌────────┴────────┐
          ▼                 ▼
   ┌────────────┐    ┌────────────┐
   │ Navigator  │    │ Extractor  │  JSON-LD → DOM → LLM fallback
   │ enqueue    │    │ → Product  │
   │ subcats +  │    └─────┬──────┘
   │ products   │          ▼
   └────────────┘    ┌────────────┐
                     │ Validator  │  (Pydantic schema)
                     │ + Dedup    │  (dedup_key, content_hash)
                     └─────┬──────┘
                           ▼
                     ┌────────────┐
                     │  Storage   │  SQLite upsert (idempotent)
                     │  + Export  │  JSON / CSV
                     └────────────┘
```

Each agent has one responsibility; the orchestrator sequences them. The
intelligence lives in the agents (and their selective LLM fallbacks); the
orchestrator is deterministic coordination. The work queue and checkpoint
tables live in SQLite, so the whole thing is resumable and observable.

### Components

| Module | Responsibility |
|--------|----------------|
| `core/models.py` | `Product` schema + normalization (the shared contract) |
| `core/config.py` | typed, validated config loader; resolves secrets from env |
| `core/fetcher.py` | async fetch: token-bucket rate limit, concurrency cap, retry/backoff; pluggable `httpx`/`playwright` |
| `core/logging_config.py` | structured JSON logging |
| `agents/navigator.py` | discovers subcategories + product URLs from listing JSON-LD |
| `agents/classifier.py` | page-type detection (rules → optional LLM tie-break) |
| `agents/extractor.py` | product extraction (JSON-LD → DOM → LLM fallback) |
| `llm/client.py` | Anthropic-backed extraction/classification fallbacks, call-capped |
| `storage/db.py` | SQLite: products, url_queue, failures, run_summary |
| `storage/export.py` | JSON / CSV export |
| `orchestrator.py` | run lifecycle, queue draining, metrics |
| `main.py` | CLI entrypoint |

---

## 4. Output schema

One row per product, keyed by `dedup_key`. Nested fields are JSON in the DB and
in the CSV; native objects/arrays in the JSON export.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `dedup_key` | string | derived | `sku:<SKU>` or, if no SKU, `url:<url>` |
| `sku` | string\|null | JSON-LD | canonical parent product code |
| `product_name` | string | JSON-LD / `<h1>` | required |
| `brand` | string\|null | JSON-LD | |
| `product_url` | string | JSON-LD | canonical detail URL |
| `price` | number\|null | JSON-LD / `.price` | parent/displayed price |
| `currency` | string\|null | JSON-LD | ISO code, e.g. `USD` |
| `availability` | enum | JSON-LD | `InStock`/`OutOfStock`/`Unknown`/… |
| `pack_size` | string\|null | DOM | when present |
| `variant_skus` | array | DOM | `[{sku, pack_size, price}]`; extension point |
| `category_hierarchy` | array | BreadcrumbList | `[{name, url}]`, root→leaf |
| `specifications` | object | DOM | key→value; empty when JS-injected |
| `image_urls` | array | JSON-LD | query params stripped |
| `alternative_products` | array | DOM | related-product URLs |
| `description` | string\|null | JSON-LD | HTML entities cleaned |
| `source_url` | string | provenance | the URL actually fetched |
| `scraped_at` | datetime | provenance | UTC |
| `extraction_method` | enum | provenance | `json_ld`/`dom_selector`/`llm_fallback`/`mixed` |
| `run_id` | string | provenance | run that produced the record |
| `content_hash` | string | provenance | hash of content fields; drives idempotency |
| `first_seen_at` / `last_updated_at` | datetime | storage | lifecycle timestamps |

---

## 5. Engineering trade-offs

* **JSON-LD-first over DOM scraping.** More robust to layout/CSS changes; the
  site maintains JSON-LD for SEO, so it's well-formed and stable.
* **Canonical parent SKU as the key, variants deferred.** Keeps every product
  uniquely addressable today without blocking on per-variant DOM parsing.
* **Deterministic before LLM.** The LLM is a fallback with a per-run call cap,
  not the engine — faster, cheaper, reproducible. `extraction_method` makes its
  usage measurable.
* **SQLite for the prototype.** Zero-setup, gives ACID upserts and a persisted
  queue for free. The schema is portable to Postgres (see below).
* **Fetcher behind an interface.** `httpx` for this SSR site; Playwright is a
  documented drop-in for JS-rendered targets, selected via config alone.

---

## 6. Production-minded design

Each item below is addressed in the prototype and noted where a production
deployment would extend it.

| Concern | In the prototype | Production path |
|---------|------------------|-----------------|
| **Rate limiting** | token bucket (req/s) + concurrency semaphore + per-host delay/jitter | per-domain budgets; distributed limiter (Redis) |
| **Retries** | exponential backoff + full jitter, transient statuses only | unchanged; add circuit-breaker per host |
| **Error handling** | every failure → `failures` table; one bad page never crashes the run | alerting on failure-rate thresholds |
| **Resumability** | `url_queue` states persisted; in-progress re-queued on restart | unchanged; queue → Redis/SQS for multi-worker |
| **Idempotency / dedup** | upsert by `dedup_key`; `content_hash` skips no-op writes | unchanged |
| **Config-driven** | all site/runtime settings in `config.yaml`; new site = new config | per-site config files; selector overrides |
| **Secrets** | env var (`.env`), never in config | cloud secret manager / vault |
| **Logging** | structured JSON, run-scoped | ship to aggregator (ELK/Datadog) |
| **Observability** | `run_summary`: pages, products, dedup hits, failures, LLM calls, duration | export metrics to Prometheus/Grafana |
| **Deployment** | runs locally as one command | Docker image; scheduled job (cron / k8s CronJob) or queue worker; DB → Postgres; metrics/alerting |

### Known extension points
* Per-variant SKU / price / pack-size parsing from the embedded Magento config.
* A real `robots.txt` parser wired to the existing `respect_robots_txt` flag.
* Specs / related products via the LLM fallback for pages where they're
  JS-injected.
* `xlsx` export (JSON/CSV satisfy the current spec).

---

## 7. Testing

The extraction and navigation agents are verified offline against saved HTML
fixtures (`fixtures/`), so tests are fast, deterministic, and need no network —
the right pattern for a scraper, since live sites change. The full pipeline
(seed → classify → navigate → extract → validate → store → export) has been run
end-to-end against fixtures and confirmed idempotent: re-processing the same
content produces no duplicates and zero new inserts.

---

## 8. Project layout

```
scraper/
├── main.py                 # CLI entrypoint
├── orchestrator.py         # control plane
├── config.yaml             # all settings (seeds, rate limits, selectors, …)
├── requirements.txt
├── .env.example            # documents the one secret
├── core/        models.py · config.py · fetcher.py · logging_config.py
├── agents/      navigator.py · classifier.py · extractor.py
├── llm/         client.py
├── storage/     db.py · export.py
└── fixtures/    regenafill.html · gloves_listing.html   (offline tests)
```
