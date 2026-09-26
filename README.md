# Dropship Manager: eBay accounting + Amazon sourcing keywords

A local app (FastAPI backend + Next.js frontend) for an Amazon → eBay dropshipping business:

- **Overview:** eBay orders synced about every 60 seconds, with net revenue after eBay fees and ad fees, Amazon cost matched from Gmail order confirmations, profit, cancellation/return handling, a revenue/cost/profit chart, an expandable orders table and a tax CSV export.
- **Sourcing:** the weekly keyword engine described below: 7 **Proven** + 7 **Explore** keywords per week, with a Candidate Explorer and History. GPT is only called when you click **Run Weekly Analysis**.

**Start here: [docs/setup.md](docs/setup.md)** covers eBay/Gmail/OpenAI credentials, install, starting the backend (`.venv/bin/uvicorn backend.app:create_app --factory --port 8000`) and the frontend (`cd frontend && npm run dev`), tests, and how the background sync works. [docs/design.md](docs/design.md) describes the sourcing engine.

Code layout: `backend/integrations/` (eBay, Gmail), `backend/services/` (sync, reconciliation, financials, sourcing), `backend/api/` (HTTP routes), `backend/db/` (SQLite + migrations), `frontend/` (UI), `main.py` (sourcing CLI).

The rest of this README documents the sourcing engine and its command-line interface, which share the app's database and eBay integration.

## Getting your API credentials

The app needs credentials from three services. Everything secret goes in `.env` or `secrets/`, both git-ignored. Never commit them or paste them anywhere public. Start by copying the template:

```bash
cp -n .env.example .env
```

### 1. eBay: `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REDIRECT_URI`, `EBAY_REFRESH_TOKEN`

These let the app read **your own seller account**: orders, payouts and fees, returns and listings. Access is read-only.

1. **Join the developer program.** Go to the [eBay Developers Program](https://developer.ebay.com/) and sign in with your eBay account (or register). Accept the API License Agreement.
2. **Create a production keyset.** Open **Hi (your name) → Application Keysets** (<https://developer.ebay.com/my/keys>) and create a **Production** keyset. The app name can be anything, e.g. `dropship-manager`.
   - eBay may first ask about **marketplace account deletion notifications** before the production keys work. Follow the prompt: either give an endpoint, or apply for the exemption if eligible.
3. **Copy the two keys into `.env`:**
   - **App ID (Client ID)** → `EBAY_CLIENT_ID`
   - **Cert ID (Client Secret)** → `EBAY_CLIENT_SECRET`
   - The Dev ID isn't needed.
4. **Create a RuName** (eBay's name for a redirect URL). On the keyset, open **User Tokens** → **Get a Token from eBay via Your Application** → **Add eBay Redirect URL**.
   - Fill in the privacy policy URL and the **auth accepted URL**. Any https page you control works, e.g. your GitHub profile; you'll only copy the address bar from it.
   - Save, then copy the **RuName** (a long string like `Your_Name-dropship-PRD-...`) into `EBAY_REDIRECT_URI`.
5. **Get the refresh token.** From the project folder, run:
   ```bash
   .venv/bin/python ebay_auth.py
   ```
   - It prints a consent link. Sign in with **your seller account** and click **Agree**.
   - eBay then redirects to your accepted URL. Copy the **entire** address from the browser and paste it into the terminal; the input is hidden.
   - The script saves `EBAY_REFRESH_TOKEN` to `.env`. It lasts about 18 months; re-run the script when it expires.
   - The consent covers the `api_scope`, `sell.fulfillment.readonly` and `sell.finances` scopes. If a token made before `sell.finances` was added is still in use, re-run the script; until then revenue is shown as an estimate.
   - Leave `EBAY_ENVIRONMENT=production`. `sandbox` is eBay's test system, with fake data.

### 2. OpenAI: `OPENAI_API_KEY`

Only needed for **Run Weekly Analysis** (sourcing keywords). The rest of the app never calls OpenAI.

1. Sign in at <https://platform.openai.com/>.
2. Add a payment method or prepaid credits under **Settings → Billing**. API usage is billed separately from ChatGPT subscriptions.
3. Open **API keys** (<https://platform.openai.com/api-keys>) → **Create new secret key**, and copy it. It's shown only once.
4. Put it in `.env` as `OPENAI_API_KEY=sk-...`.
5. The model is set by `model` in `config.json` (default `gpt-5.4-mini`). Your account must have access to it.

### 3. Google Gmail API: `secrets/gmail_client.json`

Used to read Amazon order-confirmation emails (read-only) so each order's Amazon cost is filled in automatically. Gmail uses an **OAuth client file**, not an API key.

1. **Create a project.** Go to the [Google Cloud Console](https://console.cloud.google.com/) and create a project (top bar → project picker → **New project**).
2. **Enable the Gmail API.** Open **APIs & Services → Library**, search **Gmail API**, and click **Enable**.
3. **Set up the consent screen.** Open **APIs & Services → OAuth consent screen** (or **Google Auth Platform**) and configure it:
   - User type: **External**.
   - App name and support email: anything, e.g. `dropship-manager` and your email.
   - Under **Audience / Test users**, add the Gmail address that receives your Amazon confirmations.
4. **Create the client.** Open **Credentials** (or **Clients**) → **Create credentials → OAuth client ID** and choose application type **Desktop app**. Download the JSON and save it as `secrets/gmail_client.json`; create the `secrets/` folder in the project root if needed.
5. **Connect once.** Run:
   ```bash
   .venv/bin/python -m backend.integrations.gmail.auth
   ```
   - A browser window opens. Sign in with that Gmail account and allow read-only access.
   - If Google shows "Google hasn't verified this app", choose **Advanced → Go to dropship-manager**. It's your own app.
   - The token is saved to `data/gmail_token.json`.
6. **Keep it from expiring.** While the consent screen's publishing status is **Testing**, Google expires the token every **7 days**. For unattended use, click **Publish app** so the status is **In production**; no verification is needed for personal use. Otherwise, re-run step 5 weekly.

### Personal settings (optional)

- **`config.local.json`** is a git-ignored file next to `config.json`. It holds private settings that override `config.json`. Example: to ignore Amazon orders shipped to yourself or family, create:
  ```json
  { "matching": { "personal_recipients": ["yourfirstname", "familymember"] } }
  ```
- For the rest of the setup (install, starting the backend and frontend, how the sync works), see **[docs/setup.md](docs/setup.md)**.

## Setup

Requires Python 3.11+. Run commands from this project directory.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# If .env does not already exist:
cp -n .env.example .env
```

Fill in `.env` locally:

```dotenv
OPENAI_API_KEY=your_openai_key
EBAY_CLIENT_ID=your_ebay_app_id
EBAY_CLIENT_SECRET=your_ebay_cert_id
EBAY_REFRESH_TOKEN=your_seller_oauth_refresh_token
EBAY_ENVIRONMENT=production
```

The refresh token must represent **your seller account**, with these consent scopes:

- `https://api.ebay.com/oauth/api_scope` (Trading and Browse APIs)
- `https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly` (orders)
- `https://api.ebay.com/oauth/api_scope/sell.finances` (payouts and fees)

An application-only client-credentials token does not authorize seller downloads.
Use production credentials for real listings; sandbox data is separate. Keep a separate
config/database for each seller account and environment.

If you don't have a seller refresh token yet, create an application and OAuth RuName in
the [eBay Developer portal](https://developer.ebay.com/), configure its accepted URL,
and put the RuName in `EBAY_REDIRECT_URI`. Then run:

```bash
python ebay_auth.py
```

The helper provides a consent link, asks for the full final redirect URL using hidden
terminal input, validates OAuth state, and saves the refresh token directly to `.env`.
Complete this promptly: authorization codes are short-lived. You can also obtain the
seller token using eBay's developer token flow. An existing short-lived seller token can
be supplied as `EBAY_ACCESS_TOKEN`, but refresh credentials are preferred for automation.
See [eBay's OAuth guide](https://developer.ebay.com/develop/guides/sell/authorization).

`.env`, downloads, SQLite history, and generated output are excluded from Git. The
OpenAI request includes candidate statistics and sample product titles, never buyer
contact information. Keep credentials out of chat and source control.

## Weekly workflow

```bash
python main.py --import     # Download eBay orders + active/unsold listings into SQLite
python main.py --analyze    # Inspect Python's candidates; no network call
python main.py --weekly     # Download, analyze, call GPT-5.4-mini, save 14 keywords
```

`--weekly` performs the import itself. You do not need to run the first two commands.
For a saved week's recommendations, reruns reuse the validated selection. Use
`--weekly --refresh` to regenerate that week after new data arrives. Use
`--weekly --offline` to skip eBay downloads (OpenAI is still called for an unsaved week).
`--as-of YYYY-MM-DD` analyzes through that UTC date and assigns the Monday–Sunday week
containing it. Run weekly on Monday; using a completed day's data avoids partial-day
trend estimates. A historical run uses stored historical observations, not past API
inventory snapshots that eBay cannot reconstruct.

A weekly scheduler can run, for example, this Monday morning cron entry (replace paths):

```cron
0 8 * * 1 cd /absolute/path/Keywords && .venv/bin/python main.py --weekly >> data/weekly.log 2>&1
```

Avoid overlapping runs. Exit code 0 means success; 1 means download/validation/selection
failed. A failed download never commits partial API data. Missing credentials produce an
actionable error. If GPT refuses or fails validation three times, the week is published from
the top Python scores instead, clearly marked `fallback` in the report and JSON (blank
confidence). Too few candidates still fails without saving a week or inventing filler.

## Outputs

In `output/`:

- `weekly_keywords.csv`: `day,tier,keyword,rank,confidence,total_units_sold,recent_units_sold,distinct_products,trend,reason`
  (`tier` is Proven or Explore; `rank` is the rank within the tier)
- `keyword_candidates.csv`: volume, successful/unsold listings, mean/median, product breadth,
  concentration, prior/recent units, score and example titles for each candidate.
- `weekly_report.md`: schedule with sell-through lift, NEW/RETAINED/RETURNING, DROPPED keywords,
  past keyword results and coverage notes.
- `weekly_keywords.json`: complete validated result and reproducible facts/configuration.
- `listing_metrics.csv`: listing-level total/recent/prior quantities and trend.
- `candidate_diagnostics.json`: counts remaining after each candidate filter and the active thresholds.

Normalized API downloads are also saved under `reports/YYYY-MM-DD/`. They contain no
buyer information. SQLite stores all accumulated history even after it falls outside
API lookback windows. Individual exports use atomic file replacement; interrupted
exports can be regenerated with `--weekly --offline` from the saved week.

## How data and scoring work

**Orders drive quantities.** Fulfillment `getOrders` downloads the last 90 days by
creation date and refreshes orders modified in that window, including full cancellations.
Pagination, transient retries, OAuth token refresh and Trading API error checks are built in.
Active listings and the past 60 days of ended-unsold listings come from
`GetMyeBaySelling`. This includes listings created outside eBay's Inventory API, so the
unsold denominator is not restricted to listings created by this app.

Cumulative listing `QuantitySold` counters are stored for audit, **never summed with
orders or with earlier snapshots**. Quantities are gross ordered units, excluding fully
canceled orders; returns and partial cancellations are not subtracted. Historical
transaction coverage must be sufficiently complete for zero-sales and trends to be useful.
An unsold listing here means zero measured order units in accumulated history.

Variations are aggregated to the parent eBay item ID, preventing variation counts from
inflating successful-listing breadth. Product identity is the Amazon ASIN when the SKU
carries one (an 8-hex prefix plus the base64 ASIN, e.g. `v0a1b2c3dQjBURVNUQVNJTg` →
`B0TESTASIN`), otherwise the parent SKU, otherwise a normalized title proxy. Existing
databases are upgraded automatically on the next run.

Only the last `analysis_days` (default 90) of sales are analyzed. Listing start/end dates
from `GetMyeBaySelling` give each listing its days live inside that window
(`exposure_days`); listings without dates, including those stored before this field
existed, are assumed live for the whole window until the next `--import` fills them in.

**Sell-through lift** is a keyword's units per listing-day divided by the store average,
smoothed toward 1.0 by `lift_prior_units` (2) so small samples cannot look extreme.
Keywords below `min_lift` (1.0, the store average) are removed: sourcing more of a
keyword whose existing listings sell worse than average is unlikely to help.

The default 28-day recent window is compared to the preceding 28 days. Dates are UTC,
inclusive of `--as-of`, excluding future sales. Keyword trends compare the keyword's
**share of store units** between windows, so catalog growth alone is not "rising".
Fewer than `min_trend_units` (4) units is `insufficient_data`; a zero prior share with
sales now is `new_activity`; ±20% share changes are rising/falling, otherwise flat.

Candidates are contiguous 1–3 word phrases within one title segment: `|`, commas,
parentheses and spaced dashes split titles, and a final word cut off by `...` is dropped.
Letter+digit model codes (F150, F-150, RAV4, 4Runner, CR-V, PS3) are kept when they appear
across at least `min_alnum_token_products` (5) products; rarer codes are treated as part
numbers. Quantities/sizes (`2pcs`, `10ft`, `M12x1.25`), pure numbers and stopwords act as
boundaries. `F-150`/`F150`, plurals and reorderings are merged and shown in their most
common title spelling. Single words qualify only as model codes or when listed in
`unigram_allowlist`: OEM plus a curated list of car makes and one-word models (Toyota,
Jeep, Tacoma, Camry, Wrangler, Silverado, CR-V…). Add names there to allow them; generic
single words ("plug", "light") are never candidates, so product phrases like "lug nuts" lead.
Other candidates need at least 2 units across at least 2 successful listings and 2 product
identities, with no product supplying over 75% of units. Mean/median includes unsold matched listings.
Candidates with 2–4 units carry `limited_volume` evidence labels and are exploratory leads.
The report identifies limited-volume selections; 5+ units is not a significance claim either.
The filter funnel is saved in `candidate_diagnostics.json`; sparse pools are never padded.

Default score:

```text
5 × log(1 + total units) + 3 × log(1 + recent units)
+ 2 × log(1 + successful products) + 3 × log(sell-through lift) − 3 × largest-product share
```

Revenue/profit never enter the score. Configure support thresholds, weights, the unigram
the single-word allowlist, blocked phrases, windows, lift settings and candidate limit
in `config.json`. Up to 200 qualified phrases go to GPT (sparse pools are not padded).
Before selection, a nested phrase on exactly the same listings as a shorter one replaces it
("spark plug" over "plug"). Different products that sell together ("ignition coil" and
"spark plugs") are never deduplicated: the goal is to reach more relevant listings, and each
keyword reaches different ones. Only true variants share a slot: plurals/reorderings,
synonyms and nested phrases ("spark plugs" and "iridium spark plugs"). GPT handles semantic usefulness, balances product phrases with vehicle models and resolves the
remaining nesting. GPT returns only keyword, reason and confidence; Python joins every
statistic.

**Proven and Explore tiers.** Candidates with at least `tiers.proven_min_units` (5) units are
Proven; the rest are Explore. If fewer than 7 distinct Proven keywords exist, the
highest-scoring Explore candidates are promoted. Proven picks are ranked by the main
quantity-led score. Explore candidates are ranked by `explore_score`, the same score without
the total-volume term, so sell-through and recent sales lead. Each Explore candidate also
carries `outside_proven_share`: the share of its listings that mention no proven keyword,
a hint that it is a different market. GPT returns two ranked lists of up to 10 (Proven from
Proven candidates, Explore from Explore candidates) and labels each Explore pick
`new_category` or `new_niche`. Python publishes the first 7 valid, nonredundant picks per
list, with redundancy checked across both lists. At least `tiers.explore_min_new_category`
(4) Explore picks must be `new_category`, so the store grows into more categories. Day N
pairs Proven #N with Explore #N. Explore results take 60–90 days to read, so an Explore
keyword rests for `tiers.explore_cooldown_weeks` (4) after being recommended; if too few
Explore candidates remain, the least recently explored return first. Proven keywords never
rest, and an Explore keyword that reaches 5 units becomes Proven automatically. The ASIN
counts (`tiers.proven_asins`, `tiers.explore_asins`) appear in the report header.
If fewer than 7 per tier remain after validation, GPT retries twice with the problems named,
then the marked deterministic fallback publishes:
- Proven: the top scores.
- Explore: 4 candidates whose listings never mention a proven keyword, then the best remaining.
- Phrases, model codes and allowlisted words only, since generic single words need GPT review.

**Past keyword results.** Listings started during an earlier recommendation week whose
titles contain that week's keyword are counted with their sales (the last 8 weeks). GPT
sees these results, labeled by tier, to retain Proven keywords whose new listings sold, and
the report lists them.
Attribution by title is approximate; keywords without matching listings show `no data`.

Confidence is GPT's subjective sourcing judgment, not a calibrated success probability.
Keywords are plausible Amazon title phrases, not verified live Amazon search results.
The configured `gpt-5.4-mini` model is used, with the Responses API and strict structured
outputs. Model access depends on your account; no model substitution occurs.

## Optional historical CSV/XLSX backfill

This is optional; regular operations download data automatically.

```bash
python main.py --import --files old-orders.csv another-week.xlsx
python main.py --import --files active-listings.csv --kind listings --snapshot-date 2026-09-21
```

Orders require Item Number, Item Title (or Title), Order Number, Quantity and Sale Date.
Transaction ID is recommended to distinguish multiple variation rows. Listing reports
require Item Number, Title and Sold quantity, plus an explicit snapshot date. Available
quantity is never interpreted as sales. Listing-only backfills cannot reconstruct dated
sales and will not generate positive sales evidence on their own.

IDs must remain text in Excel. Supported dates: ISO dates/timestamps, `MM/DD/YYYY`,
`Sep-21-26`, and Excel date cells. Ambiguous/unsupported date formats fail with a row
number. CSV is UTF-8/BOM supported, comma-delimited; XLSX reads the active sheet. Blank
rows and pre-header metadata are tolerated; malformed data/footer rows are rejected.
Configure nonstandard headers using `column_mapping`, e.g. `{"quantity": "Units ordered"}`.
Do not mix sellers in the same database. Reimporting the same file is a no-op and overlapping
order exports upsert by order + parent item. Conflicting file quantities/dates are rejected;
eBay is authoritative for quantity corrections. Do not mix order-summary rows with order-line
rows or import partial subsets of an order's variations.

## Tests

```bash
python -m pytest -q
```

Tests cover CSV/XLSX ingestion, ASIN identity and migration, overlap/idempotency,
transaction rollback, quantity/date windows, listing exposure, sell-through lift, trend
normalization, model-code/unigram extraction, variant dedup, redundancy, structured response
validation, fallback, past keyword results, Proven/Explore tiers, new-category minimum, Explore cooldown, exact 7-day Proven+Explore scheduling, historical
status, API pagination, OAuth refresh, cancellation corrections, API error handling and
saved-week reuse.
API tests use mocked HTTP responses. Real eBay/OpenAI calls require your credentials.

See [data model and design](docs/design.md) for field inspection and design decisions.
