# Ecommerce → Airtable Order Sync

**Every Gumroad and Shopify order, refund and chargeback lands in your Airtable base once, correctly, and stays correct, even when webhooks are missed, retried or arrive out of order.**

![tests](https://img.shields.io/badge/tests-37%20passing-brightgreen) ![python](https://img.shields.io/badge/python-3.10%2B-blue) ![n8n](https://img.shields.io/badge/n8n-workflow%20included-orange) ![airtable](https://img.shields.io/badge/Airtable-5%20req%2Fs%20safe-yellow)

## The problem

This is one of the most common automation jobs on Upwork and the n8n forum: *"sync my Gumroad / Shopify sales into Airtable"*. The first version is easy. The version that stays right is not:

- A refund webhook gets missed, so Airtable still says "paid" and the revenue numbers are wrong.
- The store retries a webhook, or an old zap ran twice, and the same order appears on two rows.
- A backfill hits Airtable's **5 requests/second** limit, gets `429`, and silently drops orders.
- One bad record (a currency that isn't a select option) makes Airtable reject the **whole batch of 10**.
- Nobody can answer *"does Airtable match Shopify right now?"*

## The solution

A small Python service (plus an importable n8n workflow) that does four jobs:

1. **Real-time webhooks**: Shopify `orders/*` + `refunds/create` (HMAC-verified) and Gumroad Ping (secret-token URL)
2. **Backfill**: pages through the Gumroad Sales API and Shopify Orders API (cursor pagination), resuming from the last cursor
3. **Upsert to Airtable**: normalises everything to one order shape and writes with `performUpsert` on `Order ID`, 10 records per call, rate-limited
4. **Reconciliation report**: compares the stores to Airtable and lists **missing, mismatched, duplicate and orphaned** rows, with an optional `--fix`

## Architecture

```mermaid
flowchart LR
    S[Shopify<br/>orders + refunds] -->|webhook, HMAC| W
    G[Gumroad<br/>sales + refunds] -->|Ping, secret token| W
    S -. backfill: orders.json<br/>Link-header pages .-> B
    G -. backfill: /v2/sales<br/>page_key .-> B
    subgraph Service [Sync service - FastAPI]
        W[Verify + dedupe<br/>webhook id ledger] --> N[Normalise<br/>one Order shape]
        B[Backfill from cursor] --> N
        N --> C{Stale or<br/>unchanged?}
        C -- yes --> K[Skip: 0 API calls]
        C -- no --> U[Upsert batches of 10<br/>merge on Order ID]
        U --> L[Token bucket 4.5 req/s<br/>429 -> 30 s pause]
    end
    L --> A[(Airtable<br/>Orders table)]
    U -. 422: isolate bad record .-> Q[(Dead-letter queue)]
    U -. still failing after retries .-> Q
    Q -->|replay after fix| U
    R[Nightly reconciliation] --> A
    R --> S
    R --> G
    R --> M[Report: missing / mismatched /<br/>duplicates / orphaned]
```

## Reliability (the part most "Gumroad → Airtable" zaps skip)

| Risk | What this project does |
|---|---|
| Airtable's 5 req/s limit | Client-side **token bucket** (`AIRTABLE_MAX_RPS=4.5`) so a big backfill never trips it. Demo: 25 writes in 5.3 s, **0** 429s |
| A `429` anyway (another tool on the same base) | All requests **pause** for `AIRTABLE_429_COOLDOWN` (Airtable asks for 30 s; `Retry-After` honoured), then retry. Demo: limiter off, 5 × 429, 0 orders lost |
| Shopify/Gumroad 5xx or timeouts | Exponential backoff with jitter. `4xx` is never retried pointlessly |
| One invalid record fails the whole batch (`422`) | The batch is retried **one record at a time**, so the 9 good rows land and only the bad one goes to the dead-letter queue |
| Webhook retries / resends | **Idempotency ledger** on `X-Shopify-Webhook-Id` (and a hash of the Gumroad Ping body). Upserts merge on `Order ID`, so a write is safe to repeat |
| Webhooks arriving out of order | Shopify: an event older than the stored `updated_at` is ignored. Gumroad: a refunded/charged-back order is never downgraded back to "paid" |
| Wasting API calls on re-runs | A **fingerprint** of the business fields is stored. Unchanged orders cost 0 Airtable requests |
| Duplicate rows already in the base | Airtable refuses to upsert when 2 rows match. The order goes to the dead-letter queue with a clear message, and the report lists both record IDs |
| Missed webhooks | Nightly **backfill + reconciliation** catches anything the webhooks didn't deliver |
| Forged webhooks | Shopify HMAC-SHA256 over the raw body, constant-time compare. Gumroad Ping URL carries a long secret token |
| Deleting the client's data | Never. Orphaned and duplicate rows are **reported**, not deleted |
| "What happened to order X?" | Structured JSON logs on every step (order key, outcome, Airtable record ID, attempt, delay) |

## Quick start (2 minutes, no API keys)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env

./scripts/demo.sh     # mock Gumroad + Shopify + Airtable, full story end to end
pytest                # 37 tests
```

Run the service (mock APIs start automatically when `MOCK_MODE=true`):

```bash
./scripts/serve.sh                    # webhooks on :8711, mock APIs on :8710
python -m ecom_sync.cli backfill      # pull everything since the last cursor
python -m ecom_sync.cli reconcile     # report (add --fix to repair missing/mismatched)
python -m ecom_sync.cli dlq           # what needs a human
python -m ecom_sync.cli replay all    # after fixing the cause
```

### With n8n

Import `n8n/ecommerce-airtable-sync.json` (**Workflows → Import from file**). It contains:

- **Shopify webhook → Verify HMAC (Code) → Normalise → Airtable upsert (merge on Order ID) → Respond**
- **Gumroad Ping → token check → Normalise → the same upsert**
- The upsert node has **Retry On Fail** (5 tries, 5 s apart) and an **error output** that builds a dead letter with the full payload, alerts Slack and responds `202`
- **Nightly 02:00**: calls the Python service's `/backfill` and `/reconcile?fix=true`, and posts to Slack only when duplicates or orphans need a human

Verified on n8n 1.123 against the mock Airtable API: see [`docs/n8n-demo-output.txt`](docs/n8n-demo-output.txt).

### Going live

```env
MOCK_MODE=false
GUMROAD_ACCESS_TOKEN=...
SHOPIFY_SHOP=your-store.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_...
SHOPIFY_WEBHOOK_SECRET=...
AIRTABLE_TOKEN=pat...            # data.records:read + data.records:write
AIRTABLE_BASE_ID=app...
```

Airtable table `Orders` fields: `Order ID` (primary, text), `Source` (single select: Gumroad, Shopify), `Order #`, `Email`, `Customer`, `Products`, `Qty` (number), `Currency` (single select), `Gross`/`Refunded`/`Net` (currency), `Status` (single select: paid, partially_refunded, refunded, chargeback, pending, cancelled), `Ordered At` (date).

## Demo output

`./scripts/demo.sh` (full output in [`docs/demo-output.txt`](docs/demo-output.txt)):

```
### 1. Reconciliation BEFORE      missing 8 · mismatched 3 · duplicate rows 1 · orphaned 1
### 2. Backfill                   gumroad: created 3, updated 3, dead_lettered 1 (JPY not a select option)
                                  shopify: created 4, updated 1, dead_lettered 1 (2 rows match #1002)
                                  Shopify page 2: 503, 503, then OK.  Airtable: one 429 -> paused -> OK
### 3. Run it again               gumroad: unchanged 6   shopify: unchanged 5   (no rewrites)
### 4. Live webhooks              orders/paid -> updated | same webhook again -> duplicate
                                  older event arriving late -> stale | bad HMAC -> 401
                                  refunds/create -> updated | Gumroad refund ping -> updated
### 6. Client fixes the base, replay   replay #1: created 1   replay #2: updated 1
### 7. Reconciliation AFTER       missing 0 · mismatched 0 · duplicates 0 · orphaned 1 (left for a human)
### 8. Rate limit                 limiter on: 25 req, 0 × 429 | limiter off: 5 × 429, all recovered
```

Example report: [`docs/reconciliation-report-example.md`](docs/reconciliation-report-example.md).

## Project structure

```
ecom_sync/
  api.py            FastAPI: /webhooks/shopify, /webhooks/gumroad, /backfill, /reconcile, /dlq
  pipeline.py       SyncEngine: normalise -> stale/unchanged check -> batch upsert -> record or dead-letter
  airtable.py       Airtable client: list (offset), upsert (performUpsert, 10/call), 429 cooldown, 422 isolation
  ratelimit.py      token bucket shared by every Airtable request
  normalize.py      Gumroad sale / Ping and Shopify order -> one Order
  reconcile.py      missing / mismatched / duplicate / orphaned report, optional --fix
  sources/          Gumroad (page_key) and Shopify (Link header) clients, Shopify HMAC check
  store.py          SQLite: webhook ledger, fingerprints, dead letters, cursors
  retry.py          backoff + jitter, Retry-After, transient vs permanent errors
mock_apis/          offline Gumroad, Shopify and Airtable APIs (real URL shapes, limits and errors)
n8n/                importable n8n workflow
tests/              37 tests (normalisation, Airtable limits, pipeline, webhooks, reconciliation)
```

## Tech

Python 3.10+, FastAPI, httpx, SQLite, pytest. n8n 1.x for the visual version.

## License

MIT, © 2026 Taha Elkarroumy
