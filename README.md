# LiveDigit Social Media Lead Finder

A Streamlit dashboard for discovering local business profiles on Instagram and Facebook in Andhra Pradesh and Telangana. It searches indexed results through Serper, records published business contacts with source URLs, and exports the filtered collection to Excel or CSV. It never sends messages or places calls.

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

Open the address printed by Streamlit. Pick a state, city, category, platforms, and languages. Use **Custom location** for another city or town within the state. Use a name in Telugu for a Telugu-only custom location. The custom keyword supplements the selected category. Choose up to five pages per platform and language and run **Find businesses**. **Search history** shows completed and partial searches. The **Business leads** tab can show all leads or a selected search, filter to records with a number, and export that view.

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

Each Serper search page and optional website lookup reserves one estimated credit. The dashboard counts attempted requests, including provider errors, and does not know the account balance or final billing. Limits reset at midnight India time. The 1,000-profile target is a ceiling. Reaching it depends on the request limit, result supply, and deduplication. Searches are user-triggered.

## Source and quality rules

- Social discovery uses Serper's organic results. The app does not request Instagram or Facebook profile pages directly. It excludes obvious posts, reels, influencers, and profiles lacking local business evidence. Results and locations are heuristic and some relevant businesses will be missed.
- Numbers must have an explicit contact label, a public `tel:` link on an authorized site, or explicit WhatsApp evidence. Indian mobile numbers are stored in `+91` format. A WhatsApp number is recorded separately and only from explicit WhatsApp text or links. No missing values are fabricated. Format validity does not establish ownership, current availability, or WhatsApp registration.
- Website URLs may come from a labeled social result or the optional Serper lookup. Lookup matches are candidates based on name, domain, and city evidence. The app reads a website only when its exact HTTPS hostname is in the operator's allowlist, its robots policy is readable and permits the request, and the destination resolves to public addresses. Website requests use direct outbound HTTPS with certificate verification against the approved hostname. The reader skips redirects, URLs with query strings, and login-protected pages. Only allowlist hosts you trust and are authorized to access.
- Treat sources as evidence for review. Keep access to the SQLite database and exported contact data limited to authorized users. The app stores one public value per contact field with its source. Shared numbers can merge businesses or branches. Each canonical profile is still counted in the platform totals.
- Excel preserves phone numbers as text. CSV uses UTF-8 and prefixes values that spreadsheet software could interpret as formulas, including numbers beginning with `+`, with an apostrophe. CSV importers can remove that safety prefix when reading fields as plain text.

## Tests

```bash
python -m pytest -q
```

The tests use mock Serper and website responses and temporary SQLite databases. They cover parsing, pagination, quota and error handling, deduplication, exports, and a Streamlit search flow. No API key or paid credits are required.

## Docker

```bash
docker build -t livedigit-lead-finder .
docker volume create livedigit-data
docker run --rm -p 127.0.0.1:8501:8501 --env-file .env \
  -e DB_PATH=/data/leads.sqlite3 -v livedigit-data:/data livedigit-lead-finder
```

If your build runs behind a managed HTTPS proxy, pass its trusted CA bundle to Docker BuildKit using `--secret id=pip_ca,src=/path/to/trusted-ca-bundle.pem` on the build command.

The container binds to `0.0.0.0:${PORT}`. Check readiness using `GET /_stcore/health`. The named volume retains leads, history, and usage across container restarts.

## Railway-compatible deployment

Only deploy when you're ready to accept the hosting provider's plan and usage costs.

1. Create a Railway service from this GitHub repository. Use the repository root and let Railway build the `Dockerfile`.
2. In the service settings, attach a persistent volume at `/data` and keep the service at one replica. Set `DB_PATH=/data/leads.sqlite3`.
3. Add `SERPER_API_KEY` and a strong `DASHBOARD_PASSWORD` in Railway Variables. Set `DAILY_API_CREDIT_LIMIT` to a value your Serper plan supports. Set `AUTHORIZED_WEBSITE_DOMAINS` only for approved hosts.
4. Set the health check path to `/_stcore/health`. If you need external access, generate a Railway HTTPS domain after setting the dashboard password.
5. Open the dashboard and check that it requires the password. Run a one-platform, one-language, one-page search to validate your Serper key if you are willing to use a credit. Export a result and verify that leads and history survive a restart before relying on the service.

If you only want to evaluate the MVP without using paid services, use the local Docker or Python workflow and the mocked test suite.
