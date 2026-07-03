# CLAUDE.md — truage-activity-report

> Repo: `paulziv/truage-activation-report` (folder is `truage-activity-report`; GitHub repo name is `truage-activation-report`).
> Deployed to Railway (TruAge/HubSpot cluster). Serves the **TruAge Activation Report** — the weekly KPI + forecast report tracking progress toward the 25,000-store goal.

## What this service is
A small Flask web app that, on demand, pulls data from HubSpot and renders an HTML report showing how many convenience stores are live on TruAge and how the activation funnel is moving. It is the source of the "Active Stores," "Committed Pipeline," and "Funnel Conversion" numbers that also surface inside pez-portal.

## Tech stack (verified)
- **Python 3.12**, **Flask 3.0** served by **gunicorn** (1 worker, 180s timeout).
- HTTP to HubSpot via `requests`. `python-dotenv` for local `.env`.
- Optional **Postgres** (`psycopg2-binary`) for run history; falls back to **SQLite** locally.
- No frontend framework — reports are server-rendered HTML strings.
- Deploy: **Dockerfile** (python:3.12-slim), `railway.json` builder=DOCKERFILE, healthcheck `/health`. Exposes `$PORT` (default 5001).

## Entry points
- `app.py` — the Flask web service (this is what Railway runs). Routes:
  - `GET /` — serves the latest generated report HTML (202 + status page if not ready yet).
  - `POST /refresh` — triggers a fresh pull + regenerate in a background thread. Optionally protected by `REFRESH_SECRET` (header `X-Refresh-Secret` or `?secret=`).
  - `GET /status` — JSON of the last run.
  - `GET /history` — recent pipeline runs (from `run_history`, Postgres/SQLite).
  - `GET /health` — Railway liveness.
- On startup `app.py` spawns one background pipeline run **if `HUBSPOT_TOKEN` is set**.

## The pipeline (how a report is built)
`app.run_pipeline()` shells out to two scripts in sequence (each `subprocess`, 120s timeout):
1. **`fetch_from_hubspot.py --output /tmp/hubspot_pull.json`** — pulls from HubSpot, writes the pull JSON.
2. **`generate_report_html.py --input … --output /tmp/latest_report.html`** — computes metrics and renders HTML.

Files live in `/tmp` (writable on Railway, reset on redeploy — fine, the report is regenerated).

## Data model — TWO HubSpot sources (this is the crux)
`fetch_from_hubspot.py` pulls both:
1. **Deals** — the `Retailer Activations` pipeline (`pipeline=default`), sales view. Deal `amount` is used as a **per-deal store count**, not dollars.
2. **Stores** — the custom **Stores** object (`STORE_OBJECT_TYPE = "2-48839355"`), operational truth: `status` (Active/Pending/Ready/…), `is_test_data`, `activated_at`, etc.

**Why both:** deal-`amount` sums say what sales thinks is closed; Store `status` says what's actually live. They diverge because Amount fields aren't updated when stores activate. The report shows the real (Store) numbers and surfaces the gap as a data-quality issue.

### STAGE_ROLES (semantic stage → HubSpot stage ID)
Stage *labels* are pulled live from HubSpot (renames absorb automatically), but which stage means what is hardcoded in `fetch_from_hubspot.py`:
- `active` = `["closedwon"]`
- In Lab = `1270202953`, Awaiting SW = `1270163972`, Awaiting Activation = `1270128498`, Awaiting Transactions = `1270078996`, Onboarding = `contractsent`
- Early-funnel (excluded from Committed Pipeline) = leads `1346410815`, unqualified `1350980982`, `qualifiedtobuy`, `appointmentscheduled`, `presentationscheduled`, `decisionmakerboughtin`, parking-lot `1335845536`.
If any role-assigned stage ID disappears from the live pipeline, the fetch **fails loudly** (exits) rather than silently reporting 0 for that KPI.

## KPI definitions & gotchas (authoritative — see also pez-portal/docs/REPORTS.md)
- **Active Stores** has two possible sources that disagree *by design*:
  - `active_stores_real` = count of Store records with `status == "Active"` (test-excluded). **Preferred** when store data is present.
  - `active_stores_deals` = sum of deal `amount` in the `closedwon` stage (test-excluded). Used only as a fallback when there's no store data.
  - `M["active_stores"]` = real count if store data present, else the deal sum. Every other number on the page keys off this single value.
- **Committed Pipeline** (stacked bar) = stores across the mid-funnel stages. `committed_stores = sum(deal amount for non-closedlost, non-early-funnel, non-active, non-test deals) + active_stores`. Note it *adds `active_stores`* rather than re-summing closedwon amounts — that reconciliation is why the 2026-07-01 report showed 9,949 vs 9,839.
- **Funnel Conversion Rate** = `round(100 * active_stores / committed_stores)`.
- **Goal** = the only hardcoded figure: `GOAL = 25_000` by `Dec 31, 2026`.
- **Test-record exclusion (two different mechanisms):**
  - *Deals*: `Deal.is_test_record()` — name-based. `TEST_EXACT_NAMES` (tester, rita, clover, …) and `TEST_SUBSTRING_PATTERNS` (thinksys, demo unit, …) in `generate_report_html.py`.
  - *Stores*: the `is_test_data` field is authoritative — **no name matching**. A store literally named "…Test…" with `is_test_data=false` is treated as real.

## Reliability history (don't regress these)
- **2026-07-01 incident:** a burst of HubSpot 429s made the Stores fetch return empty, which downstream treated as valid → ~1,600-store phantom swing. Fix: `_request_with_retry` in `fetch_from_hubspot.py` now **raises after MAX_RETRIES** (never returns None/empty). Store data is all-or-nothing: every page returns, or the whole run fails and no report is produced. Also adds 0.15s inter-page pacing.
- **Committed Pipeline / Funnel reconciliation:** test exclusion must be applied in `stage_sum()` too, and the funnel denominator reuses `active_stores` rather than a second "active" number. Keep these aligned.

## The other scripts (not run by Railway)
- `generate_report.py` — **local PDF** generator. Run by `run_report.bat` after the HTML one. Not used in production.
- `generate_period_report.py` — additive fixed-window report (HTML + CSVs). Standalone/manual.
- `compare_asof.py` — reconstructs "as of <date>" metrics from ONE pull by reusing `generate_report_html.py`'s `load_data()/compute_metrics()`. Caveat: no historical snapshots exist, so it can't reflect deleted/merged/re-edited deals.
- `run_history.py` — structured run log (Postgres via `DATABASE_URL`, else SQLite `data/run_history.db`). Same pattern as truage-pulse `pulse/storage.py`.
- `alerting.py` — crash alerts via **Resend** (same provider pez-portal uses), rate-limited per error kind. No-ops without `RESEND_API_KEY`.

## Run locally
```bash
export HUBSPOT_TOKEN="pat-na1-..."     # private app token
python fetch_from_hubspot.py --output hubspot_pull.json
python generate_report_html.py --input hubspot_pull.json --output report.html
# or run the web app:
python app.py    # serves on :5001
```
HubSpot token scopes: `crm.objects.deals.read`, `crm.schemas.deals.read`, `crm.objects.custom.read`, `crm.schemas.custom.read`.

## Deploy
Push to `main` → Railway auto-deploys (Docker). Set env in Railway: `HUBSPOT_TOKEN` (required), optional `REFRESH_SECRET`, `RESEND_API_KEY`/`RESEND_FROM_EMAIL`/`ALERT_EMAIL`, `DATABASE_URL`.

## Environment variables
| Var | Purpose |
|---|---|
| `HUBSPOT_TOKEN` | Required. HubSpot private-app token for the pull. |
| `REFRESH_SECRET` | Optional. Gates `POST /refresh`. |
| `RESEND_API_KEY` | Optional. Enables crash-alert emails (Resend). |
| `RESEND_FROM_EMAIL` / `ALERT_EMAIL` | Optional. Alert from/to (defaults: alerts@mytruage.org / ziv.paul@gmail.com). |
| `DATABASE_URL` | Optional. Postgres for run history; SQLite fallback if unset. |
| `PORT` | Set by Railway. |

## ⚠️ Security / cleanup debt
- **`run_report.bat` line 32 held a live HubSpot token in plaintext** (`HUBSPOT_TOKEN_EMBEDDED=pat-na1-…`). The file is **git-ignored** (never in GitHub history), but the token is live — it has been replaced with the `pat-na1-REPLACE-WITH-YOUR-TOKEN` placeholder so the script falls through to the `HUBSPOT_TOKEN` env var. **Action: rotate the token in HubSpot** and set `HUBSPOT_TOKEN` as a user env var (it was reused as the shared token across both TruAge services).
- Cruft to consider deleting: `generate_report_html.py-old`, `…-old1`, `…-old2`, and the `apply_*.sh` one-off patch scripts once confirmed applied.


---

## ✅ Phase 1 complete — truage-core adoption (2026-07-03)
HubSpot access, KPI constants, and test-record rules now come from the shared **truage-core**
package (`truage_core.config`, `truage_core.testrecords`, `truage_core.hubspot`), not local copies:
- `fetch_from_hubspot.py` is now thin — `get_client()` + `pull.fetch_stage_labels/fetch_all_deals/fetch_all_stores`. The in-file HTTP retry + fetchers were removed (~234 lines).
- `generate_report_html.py` imports `STAGE_ROLES`, `GOAL`, `EARLY_FUNNEL_STAGES` from `truage_core.config`; `is_test_record`/`is_test_store` delegate to `truage_core.testrecords`.
- Installed via `requirements.txt` (`truage-core @ git+https://${TRUAGE_CORE_PAT}@github.com/paulziv/truage-core@v0.1.0`); the Dockerfile installs `git` so pip can fetch it.
- Verified byte-identical before/after via `tests/characterization/` (incl. a live old-vs-new pull diff). Token env: `HUBSPOT_TOKEN` (falls back to `HUBSPOT_PRIVATE_APP_TOKEN`).
