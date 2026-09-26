# Setup and running the app

A local app for your Amazon → eBay dropshipping business:

- **Overview:**
  - eBay orders with net revenue (after eBay fees, ad fees and eBay-collected tax)
  - Amazon cost, matched from your Gmail order confirmations
  - profit, returns and cancellations
  - a chart, an orders table and a tax CSV export
- **Sourcing:** the existing weekly keyword engine: 7 Proven + 7 Explore picks, Candidate Explorer and History.

Everything runs on your Mac. Data lives in `data/sourcing.sqlite3`; credentials live in `.env` and `secrets/`, neither of which is committed.

## 1. Install

Requires Python 3.11+ and Node 20+. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp -n .env.example .env
cd frontend && npm install && cd ..
```

## 2. eBay credentials

1. Create (or reuse) a production keyset at the [eBay Developer Program](https://developer.ebay.com/). Put its App ID and Cert ID in `.env` as `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET`.
2. Under **User Tokens**, create a RuName (redirect URL name) and put it in `EBAY_REDIRECT_URI`.
3. Run the one-time seller consent helper and sign in with **your seller account**:
   ```bash
   .venv/bin/python ebay_auth.py
   ```
   It saves `EBAY_REFRESH_TOKEN` to `.env`. The consent asks for these scopes:
   - `api_scope`: listings (Trading API) and categories (Browse API)
   - `sell.fulfillment.readonly`: orders, buyer name and address
   - `sell.finances`: net payouts, final value fees, ad fees, refunds

**Already connected before this app existed?** Your refresh token predates the `sell.finances` scope, so run `ebay_auth.py` once more. Until you do, orders keep syncing, net revenue is shown as a fee-based **estimate**, and the sync panel reports the missing consent.

Returns come from eBay's Post-Order API. If your account's token can't access it, the sync panel shows an error for the returns step. In that case set return status by hand in the order row; full refunds are still detected from the order's payment status.

## 3. Gmail (Amazon order confirmations)

Amazon cost is the **Grand Total** of the matching Amazon order-confirmation email. The app reads Gmail read-only through the Gmail API (not IMAP).

1. In the [Google Cloud Console](https://console.cloud.google.com/), create a project and enable the **Gmail API**.
2. Configure the **OAuth consent screen** (External, your Google account as a test user).
3. Create an **OAuth client ID** of type **Desktop app**. Download the JSON to `secrets/gmail_client.json`.
4. Connect once; a browser window opens for read-only consent:
   ```bash
   .venv/bin/python -m backend.integrations.gmail.auth
   ```
   The token is stored at `data/gmail_token.json`.

Note that Google expires refresh tokens after 7 days for apps left in **Testing** status. If Gmail stops syncing weekly, either re-run the command above or set the consent screen's publishing status to **In production**; an unverified personal app is fine for your own account.

**Check the parser on your first real emails.** Amazon's email layout isn't something the app could see in advance. After the first sync, expand a few orders. Each email shows `parsed` (recipient and Grand Total found), `partial` (Grand Total only, so you can link it manually) or `failed`. If most are partial or failed, send a redacted example so the parser patterns in `backend/integrations/gmail/amazon_parser.py` can be adjusted.

## 4. OpenAI key

Put your key in `.env` as `OPENAI_API_KEY`. It is used **only** when you click **Run Weekly Analysis** (and confirm), or run `python main.py --weekly`. The background sync never calls OpenAI. The model is set in `config.json` (`model`).

## 5. Start the backend

From the repository root:

```bash
.venv/bin/uvicorn backend.app:create_app --factory --port 8000
```

- On first start, the database is migrated to the new schema. **A backup copy is written to `data/backups/` first**, and existing sourcing data (listings, sales, saved weeks) is kept.
- Don't use `--reload`; it would run two sync loops.
- API docs: http://127.0.0.1:8000/docs

## 6. Start the frontend

```bash
cd frontend
npm run dev          # http://localhost:3000
```

For a production build: `npm run build && npm start`. Both listen only on this Mac (127.0.0.1). The frontend forwards `/api/*` to `http://127.0.0.1:8000`. To point it elsewhere, set `BACKEND_URL` when you **build** (`BACKEND_URL=http://127.0.0.1:9000 npm run build`); `next start` ignores it because the address is fixed at build time.

## 7. Run the tests

```bash
.venv/bin/python -m pytest -q                 # backend + sourcing engine
cd frontend && npm run typecheck && npm run build
```

The tests use synthetic data and never contact eBay, Google or OpenAI.

## How the background sync works

While the backend runs, a background task runs one **sync cycle about every 60 seconds** (`config.json → sync.interval_seconds`) in a worker thread, so the API stays responsive. A lock prevents overlapping cycles, and **Sync Now** in the header just starts the next cycle immediately. Each cycle runs these steps:

| Step | What it does |
|---|---|
| eBay orders | Orders modified since the last successful sync (5-minute overlap). New orders appear immediately with revenue estimated from the order. |
| Order history | One-time backfill since `accounting.start_date`, a few 30-day chunks per cycle; a separate step, so it never blocks live orders. |
| eBay finances | Payout transactions since the last sync (2-day overlap for late postings). They replace the estimate with the actual net amount and the fee and ad-fee breakdown. |
| Finance history | One-time backfill of payout transactions since `accounting.start_date`. |
| eBay returns | Return status, every 10 minutes. |
| Categories | Looks up the eBay category of newly sold items, cached. |
| Listings | Refreshes active and unsold listings for sourcing, once every 24 hours. |
| Gmail | New Amazon confirmation emails since the last check. Each Gmail message is stored once. |
| Amazon matching | Scores unmatched orders against unclaimed emails and attaches the Grand Total when the match is clear. |

**History backfill.** On the first run, orders, finances and Gmail are backfilled from `accounting.start_date` (2026-01-01), a few chunks per cycle. The backfill resumes where it left off after a restart. Sourcing analysis still uses only the last 90 days, and orders known only from the backfill are tagged so they never change sourcing results.

**Safe to repeat.** Every write is keyed on eBay's or Gmail's own IDs (order + line item, finance transaction, Gmail message), so repeated or overlapping downloads never create duplicates.

**Failures are isolated.** Each step records its own success or error. The header's sync button shows any failing step, and the other steps keep running. For example, Gmail not yet connected doesn't stop eBay syncing.

**Matching rules.** Quantity, recipient first name, city, state and how soon the Amazon email followed the eBay sale, within −15 minutes to +24 hours, each add or subtract points; the weights are in `config.json → matching`.
- An order is matched automatically only when one email clearly wins: score ≥ 80, 15+ points ahead of any other.
- Close calls become **Needs review**, including when two orders compete for one email. Orders with no candidate stay **Pending**. They are re-checked every cycle for 26 hours, and after that whenever a new Amazon email arrives inside their window, which covers the history backfill and Gmail outages. **Recheck Amazon match** re-scores an order at any time.
- Unmatched orders show *Amazon cost unknown*, and their profit is never guessed.
- You can enter a cost by hand, link a different email, or unlink a wrong match. Manual decisions are never overwritten.

**Replacing the poller later.** `backend/services/sync/orchestrator.run_cycle()` holds all the logic. A hosted scheduler or eBay webhooks can call it without changes.

## Money rules

- **Net revenue** is what eBay actually pays out: the Finances payout after final value fees, ad fees, eBay-collected sales tax, refunds and other deductions. Raw eBay values are stored separately and shown in the order's financial breakdown and in the CSV.
- **Profit** = net revenue − Amazon cost.
- **Cancelled**, **Return completed** and **Refunded** orders count $0 revenue, cost and profit in all totals. Their original values stay visible in the order row and CSV.
- **Return started** keeps its profit and is flagged.
- **Return cancelled** goes back to normal.
- Summary cards count orders with unknown Amazon cost in orders and revenue, not in cost or profit, and show how many are awaiting a cost.

## Command line (still supported)

The original sourcing CLI uses the same database and eBay integration:

```bash
.venv/bin/python main.py --import                     # download orders + listings
.venv/bin/python main.py --analyze                    # candidates only, no OpenAI
.venv/bin/python main.py --weekly --refresh --offline # regenerate this week's picks (calls OpenAI)
```
