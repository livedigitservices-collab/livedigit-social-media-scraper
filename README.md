# LiveDigit Social Media Lead Finder

A Streamlit dashboard for discovering local business profiles on Instagram, Facebook, and Google Maps in Andhra Pradesh and Telangana. It uses Serper Search and Places, records published business contacts with source URLs, and exports the filtered collection to Excel or CSV. It never sends messages or places calls.

## Run locally

Use Python 3.12. From the repository root on Linux or macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `SERPER_API_KEY` in the local `.env` file using your own Serper account. The dashboard can open with an empty key and disables new searches in that case. Keep the file private and, when using the commands below, use shell-compatible assignments.

```bash
set -a
source .env
set +a
streamlit run app.py --browser.gatherUsageStats=false
```

Open the address printed by Streamlit. Pick a state, city, category, and languages. Choose Instagram, Facebook, Instagram + Facebook, Google Maps, or Combined business search. Use **Custom location** for another city or town within the state. Use a name in Telugu for a Telugu-only custom location. The custom keyword supplements the selected category. Choose up to three keyword/location variants and five pages per query, or enable **Bulk search** for up to eight variants and ten pages per query. A single run can accept up to 1,000 unique profile or listing URLs. Serper availability and the daily request/profile caps can stop it earlier, and several profiles can refer to the same business. Inspect **Preview search queries** and the estimated credit upper bound before clicking **Find businesses**. Optional official website lookups have a separate per-search cap and run after discovery. Use **Clear filters** to restore defaults. Each new run shows its own results with the contact-only and source filters reset. **Search history** shows completed and partial searches. The **Business leads** tab can show all leads or a selected search, filter to records with a number, and export that view.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `SERPER_API_KEY` | Empty | Required for search. Supply securely in the environment. |
| `DAILY_API_CREDIT_LIMIT` | `50` | Local ceiling on Serper request reservations per day. Set it within your provider limits. |
| `DAILY_PROFILE_TARGET` | `1000` | Maximum new canonical profile URLs recorded per day. |
| `SERPER_MIN_INTERVAL` | `1` | Minimum seconds between scheduled Serper requests on this database. Range is 0.25–60. |
| `DB_PATH` | `data/leads.sqlite3` | SQLite file. Its directory must be writable. |
| `AUTHORIZED_WEBSITE_DOMAINS` | Empty | Comma-separated exact HTTPS hostnames where the operator has permission to read published contacts, such as `example.org,www.example.org`. |
| `DASHBOARD_PASSWORD` | Empty locally | Shared password for opening the dashboard. Required in Railway. Use a strong unique value for any shared instance. |
| `PORT` | `8501` in Docker | HTTP port. Railway supplies this automatically. |

Each Search or Places page and optional website lookup reserves one estimated request credit. Empty or repeated pages end pagination for that query. The dashboard counts attempted requests, including provider errors, and does not know the account balance or final billing. Limits reset at midnight India time. The 1,000-profile target is a ceiling across profile and listing URLs. Reaching it depends on the request limit, result supply, and deduplication. Searches are user-triggered.

Open **Search diagnostics** for raw, accepted and rejected result counts, rejection reasons, API errors, actual attempted requests, and the share of deduplicated businesses linked to the run that currently have a number. Accepted counts are unique profile or listing URLs within the run. Rejected counts include repeated results. Optional website lookup results are excluded from raw discovery counts. Phase 1 runs have no recorded raw result or rejection history.

## Phase 1 production investigation

The reported run had 13 request attempts, three discovered profiles, and no collected numbers. The repository contained no production database, Serper response samples, exact query history, or per-result decisions. The three production totals alone cannot establish how many results fell into each failure mode.

| Reproduced or inspected behavior | Phase 2 change |
| --- | --- |
| A Hyderabad restaurant search generated one category term per source and language. Search results returned as organic snippets only. | Expand category synonyms and location variants. Use the Serper Places endpoint for Google Maps business listings. |
| A real-looking indexed business profile without a city in the snippet was rejected even though the query specified the city. Facebook numeric business page URLs were rejected before content was considered. | Accept public numeric Facebook page URLs with business evidence. Accept a sparse social result when a stored local listing corroborates the normalized business name and website domain or a published number. |
| The parser missed parenthesized Indian numbers, area-code landlines, and action phrases such as “Call or WhatsApp for bookings”. It had no structured business number field. | Parse these public contact formats and the Places `phoneNumber` field. Keep separate source URLs for mobile, landline and WhatsApp and leave ownership unverified. |
| Requests counted pages and optional per-profile lookups alike. Early website lookups could spend the remaining budget, and repeated pages had no stopping rule apart from the page limit. The first provider error could halt other queries. | Preview request estimates, run discovery first, cap optional lookups, stop duplicate pages, show rejection reasons, and continue independent searches after recoverable errors. |
| Phone-only global deduplication could merge branches that shared a switchboard. | Scope number-based matching to the normalized business name and city/state, and keep distinct Maps listings separate. |

## Source and quality rules

- Social discovery uses Serper's organic results. The app does not request Instagram or Facebook profile pages directly. It excludes obvious posts, reels, influencers, and profiles lacking local business evidence. Maps discovery uses Serper Places responses and never fetches Google Maps directly. Results and locations are heuristic and some relevant businesses will be missed.
- Numbers must have an explicit contact label, a public `tel:` link on an authorized site, or explicit WhatsApp evidence. Indian mobile numbers and nationally formatted landlines are stored in `+91` format. Places numbers are retained only when a listing can be linked by CID or place ID. A generic Maps search link may still identify a candidate without a number. A WhatsApp number is recorded separately and only from explicit WhatsApp text or links. No missing values are fabricated. Format validity does not establish ownership, current availability, or WhatsApp registration.
- Website URLs may come from a labeled social result, a Places listing, or the optional Serper lookup. Lookup matches are candidates based on name, domain, and city evidence. The app reads a website only when its exact HTTPS hostname is in the operator's allowlist, its robots policy is readable and permits the request, and the destination resolves to public addresses. Website requests use direct outbound HTTPS with certificate verification against the approved hostname. The reader skips redirects, URLs with query strings, and login-protected pages. Only allowlist hosts you trust and are authorized to access.
- Treat sources as evidence for review. Keep access to the SQLite database and exported contact data limited to authorized users. The app stores one public value per contact field with its source. Matching is heuristic. Businesses without corroborating signals can remain separate. Canonical profile and listing URLs remain registered for deduplication.
- Excel preserves phone numbers as text. CSV uses UTF-8 and prefixes values that spreadsheet software could interpret as formulas, including numbers beginning with `+`, with an apostrophe. CSV importers can remove that safety prefix when reading fields as plain text.

## Tests

```bash
python -m pytest -q
```

The tests use mock Serper and website responses and temporary SQLite databases. They cover query previews, Places and social parsing, repeat-page detection, source attribution, conservative matching, SQLite migration, quotas, exports, and the Streamlit search flow. No API key or paid credits are required.

## Docker

```bash
docker build -t livedigit-lead-finder .
docker volume create livedigit-data
docker run --rm -p 127.0.0.1:8501:8501 --env-file .env \
  -e DB_PATH=/data/leads.sqlite3 -v livedigit-data:/data livedigit-lead-finder
```

The container binds to `0.0.0.0:${PORT}`. Check readiness using `GET /_stcore/health`. The named volume retains leads, history, and usage across container restarts.

## Railway-compatible deployment

Only deploy when you're ready to accept the hosting provider's plan and usage costs.

1. Create a Railway service from this GitHub repository. Use the repository root and let Railway build the `Dockerfile`.
2. In the service settings, attach a persistent volume at `/data` and keep the service at one replica. Set `DB_PATH=/data/leads.sqlite3`.
3. Add `SERPER_API_KEY` and a strong `DASHBOARD_PASSWORD` in Railway Variables. Set `DAILY_API_CREDIT_LIMIT` to a value your Serper plan supports. Set `AUTHORIZED_WEBSITE_DOMAINS` only for approved hosts.
4. Set the health check path to `/_stcore/health`. If you need external access, generate a Railway HTTPS domain after setting the dashboard password.
5. Open the dashboard and check that it requires the password. Run a one-platform, one-language, one-page search to validate your Serper key if you are willing to use a credit. Export a result and verify that leads and history survive a restart before relying on the service.

If you only want to evaluate the MVP without using paid services, use the local Docker or Python workflow and the mocked test suite.
