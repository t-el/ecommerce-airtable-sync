# Handoff: Ecommerce → Airtable Order Sync

## What it does
Keeps the Airtable `Orders` table in step with Gumroad and Shopify: new orders, payments, refunds and chargebacks. Webhooks give real-time updates; a nightly backfill and reconciliation catch anything a webhook missed. It never deletes rows in Airtable.

## Where things live

| Thing | Location |
|---|---|
| Config | `.env` (every option documented in `.env.example`) |
| State | `data/sync.db`. Tables: `webhook_events` (idempotency), `orders` (last synced fingerprint, status, Airtable record ID), `dead_letters`, `cursors` (backfill position) |
| Reports | `data/reports/reconciliation-report.md` and `.json` |
| Webhook URLs | Shopify: `https://<host>/webhooks/shopify` (topics `orders/create`, `orders/updated`, `orders/paid`, `orders/cancelled`, `refunds/create`). Gumroad Ping: `https://<host>/webhooks/gumroad?token=<GUMROAD_WEBHOOK_TOKEN>` |

## Outcomes you'll see in logs

| Outcome | Meaning | Who acts |
|---|---|---|
| `created` / `updated` | Written to Airtable | Nobody |
| `unchanged` | Nothing changed since the last sync, so no API call was made | Nobody |
| `stale` | An older event arrived after a newer one and was ignored | Nobody |
| `duplicate` | The store re-sent a webhook we already processed | Nobody |
| `invalid` | The payload couldn't be read (missing email, bad amount). In the dead-letter queue | Check the order in the store |
| `dead_lettered` | Airtable rejected it, or it kept failing after retries | See the playbook below, then `replay` |

## Daily / weekly routine
1. Nightly: `python -m ecom_sync.cli backfill` then `python -m ecom_sync.cli reconcile --fix` (cron, or the n8n schedule branch).
2. Read the report. **Duplicates**: delete the extra row in Airtable, then `replay all`. **Orphans**: usually test orders deleted in Shopify; delete or keep them by hand.
3. `python -m ecom_sync.cli dlq` should be empty. If not, use the playbook.

## Failure playbook

| Dead-letter error | Cause | Fix |
|---|---|---|
| `Insufficient permissions to create new select option "JPY"` | A new currency/status that isn't a select option | Add the option to the field in Airtable (or set `typecast` in `airtable.py`), then `replay <id>` |
| `Cannot update more than one record with performUpsert` | Two rows share the same `Order ID` | Delete the extra row, then `replay <id>` |
| `retries exhausted` + `HTTP 429` | Something else is hammering the base | Lower `AIRTABLE_MAX_RPS`, find the other integration, then `replay all` |
| `AUTHENTICATION_REQUIRED` / `HTTP 401` | Token expired or lost a scope | Create a new PAT with `data.records:read/write` on the base |
| `missing customer email` | A POS or draft order without email | Expected for some POS orders; resolve manually |

## Tuning
- `AIRTABLE_MAX_RPS` (default 4.5). If another tool also writes to the base, share the budget (e.g. 3).
- `RETRY_ATTEMPTS`, `RETRY_BASE_DELAY` for store APIs.
- Add a field: map it in `Order.business_fields()` and both normalisers, then add it to `COMPARED` in `reconcile.py` if it should be checked.

## Security
- `.env` and `data/` are git-ignored. Use a Shopify custom app with `read_orders` only and an Airtable PAT scoped to this one base.
- Shopify webhooks are verified with HMAC over the raw body. Gumroad doesn't sign pings, so keep the token long and rotate it if the URL leaks.
- Run behind HTTPS (Caddy, Cloudflare Tunnel, or your platform's TLS).
