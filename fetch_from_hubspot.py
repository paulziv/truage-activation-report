#!/usr/bin/env python3
"""
fetch_from_hubspot.py
=====================
Pulls HubSpot data needed for the TruAge Activation Report:

  1) DEALS — every deal in the Retailer Activations pipeline (sales pipeline view)
  2) STORES — every record from the custom Stores object (operational state view)

Writes hubspot_pull.json — the input file for generate_report.py.

WHY BOTH:
  Deal-Amount sums tell us what the sales team thinks is closed; Store status
  fields tell us what's actually live and transacting. They diverge in practice
  because Amount fields aren't always updated when stores activate. Pulling
  both lets the report show real numbers (Active/Pending/Ready from Stores) AND
  surface the gap as a data-quality issue.

USAGE:
    export HUBSPOT_TOKEN="pat-na1-..."
    python fetch_from_hubspot.py
    python fetch_from_hubspot.py --output foo.json --report-date 2026-05-06

REQUIREMENTS:
    pip install requests

Token scopes needed:
    - crm.objects.deals.read
    - crm.schemas.deals.read
    - crm.objects.custom.read       (for the Stores custom object)
    - crm.schemas.custom.read       (for the Stores schema)
"""
import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# ============================================================
# CONFIG — Retailer Activations pipeline (Deals)
# ============================================================
PIPELINE_ID = "default"

# STAGE ROLES — the only stage IDs the report semantically depends on.
# Labels for ALL stages are pulled dynamically from HubSpot at fetch time
# (so renames absorb automatically). But the report needs to know which
# specific stages mean "Active", "In Lab", "Awaiting SW", etc. — those
# semantic assignments live here. If any of these IDs disappears from the
# pipeline, the fetch fails loudly so we don't silently zero out a KPI.
STAGE_ROLES = {
    "active_stage_ids":              ["closedwon"],
    "in_lab_stage_id":               "1270202953",
    "awaiting_sw_stage_id":          "1270163972",
    "awaiting_activation_stage_id":  "1270128498",
    "awaiting_transactions_stage_id":"1270078996",
    "onboarding_stage_id":           "contractsent",
}

DEAL_PROPERTIES = [
    "dealname",
    "dealstage",
    "amount",
    "hubspot_owner_id",
    "blocked_reason",
    "createdate",
    "closedate",
    "notes_next_activity_date",
    "hs_v2_date_entered_current_stage",
    "hs_v2_date_entered_closedwon",
    "hs_v2_date_entered_closedlost",
    "hs_v2_date_entered_1270078996",   # Awaiting Transactions
    "hs_v2_date_entered_1270128498",   # Awaiting Activation
    "hs_v2_date_entered_1270163972",   # Awaiting SW
    "hs_v2_date_entered_1270202953",   # In Lab
    "hs_v2_date_entered_contractsent", # Onboarding Began
    "hs_v2_date_entered_qualifiedtobuy",
    "hs_v2_date_entered_appointmentscheduled",
    "hs_v2_date_entered_presentationscheduled",
    "hs_v2_date_entered_decisionmakerboughtin",
    "hs_v2_date_entered_1335845536",   # Parking Lot 3 Other
    "hs_v2_date_entered_1346410815",   # Leads
    "hs_v2_date_entered_1350980982",   # Unqualified
]

# ============================================================
# CONFIG — Stores custom object
# ============================================================
# Object type ID for the custom Stores object in HubSpot.
# Find this in HubSpot: Settings → Objects → Custom Objects → click Stores
# (or it's in the URL of the property settings page: type=2-XXXXXXXX).
STORE_OBJECT_TYPE = "2-48839355"

# Properties on Store records. Internal names confirmed from HubSpot Settings.
# Display label → internal name mapping:
#   Status              → status
#   Is Test Data        → is_test_data
#   Activated At        → activated_at
#   Last Transaction    → lasttransactiondate
#   Organization Id     → organization_id
#   Store Name          → legal_name           (HubSpot internal name)
#   Store Brand Name    → store_brand_name
#   DG Store Id         → external_id          (HubSpot internal name)
#   Owner               → hubspot_owner_id
#   Object create date  → hs_createdate        (custom objects use hs_ prefix)
STORE_PROPERTIES = [
    "status",
    "is_test_data",
    "activated_at",
    "lasttransactiondate",
    "organization_id",
    "legal_name",
    "store_brand_name",
    "external_id",
    "hubspot_owner_id",
    "hs_createdate",
]

DEAL_SEARCH_URL  = "https://api.hubapi.com/crm/v3/objects/deals/search"
STORE_SEARCH_URL = f"https://api.hubapi.com/crm/v3/objects/{STORE_OBJECT_TYPE}/search"
PAGE_SIZE = 200


MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 1.0


def _request_with_retry(method, url, headers, *, json_body=None, label=""):
    """POST/GET to HubSpot with retry + exponential backoff.

    Handles the failure modes that caused the 2026-07-01 incident (a burst
    of 429s during a shared cron trigger silently produced an empty Stores
    result, which downstream got treated as valid data). This function
    NEVER returns None on failure — after exhausting MAX_RETRIES it raises,
    so callers fail loudly instead of silently degrading. That's
    deliberate: a report built from partial data is worse than no report,
    since it can misrepresent real numbers without any visible indication.

    - 429: respects the Retry-After header when HubSpot sends one,
      otherwise falls back to exponential backoff.
    - 5xx / network errors: exponential backoff with jitter.
    - other 4xx: non-retryable, raises immediately (retrying won't help).
    """
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if method == "POST":
                resp = requests.post(url, json=json_body, headers=headers, timeout=30)
            else:
                resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"WARNING ({label}): network error on attempt {attempt}/{MAX_RETRIES}: {exc}. "
                  f"Retrying in {wait:.1f}s.", file=sys.stderr)
            time.sleep(wait)
            continue

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"WARNING ({label}): 429 rate-limited on attempt {attempt}/{MAX_RETRIES} "
                  f"(Retry-After={retry_after!r}). Waiting {wait:.1f}s.", file=sys.stderr)
            time.sleep(wait)
            continue

        if 500 <= resp.status_code < 600:
            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            print(f"WARNING ({label}): HubSpot returned {resp.status_code} on attempt "
                  f"{attempt}/{MAX_RETRIES}. Retrying in {wait:.1f}s.", file=sys.stderr)
            print(resp.text, file=sys.stderr)
            time.sleep(wait)
            continue

        # Non-retryable client error (400, 401, 403, 404, etc.) — retrying
        # won't fix a bad token or a malformed request. Fail immediately.
        print(f"ERROR ({label}): HubSpot API returned {resp.status_code} (non-retryable).",
              file=sys.stderr)
        print(resp.text, file=sys.stderr)
        raise RuntimeError(f"{label}: HubSpot API returned {resp.status_code} (non-retryable)")

    raise RuntimeError(
        f"{label}: exhausted {MAX_RETRIES} retries against HubSpot API"
        + (f" (last error: {last_exc})" if last_exc else "")
    )


def hs_post(url, body, headers, *, label=""):
    """POST to HubSpot with retry/backoff. Raises on failure — never
    returns None. See _request_with_retry for retry policy."""
    return _request_with_retry("POST", url, headers, json_body=body, label=label)


def hs_get(url, headers, *, label=""):
    """GET from HubSpot with retry/backoff. Raises on failure — never
    returns None. See _request_with_retry for retry policy."""
    return _request_with_retry("GET", url, headers, label=label)


def fetch_stage_labels(token):
    """Pull live stage definitions for the Deals pipeline from HubSpot.

    Returns: dict mapping stageId → display label.
    Validates that every stage ID referenced in STAGE_ROLES actually exists
    in the live pipeline. If any role-assigned ID is missing, prints a clear
    error and exits — better to fail loud than silently report 0 for a KPI.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"https://api.hubapi.com/crm/v3/pipelines/deals/{PIPELINE_ID}"
    data = hs_get(url, headers, label="pipeline-stages")

    stages = data.get("stages", [])
    if not stages:
        print(f"ERROR: pipeline '{PIPELINE_ID}' returned no stages.",
              file=sys.stderr)
        sys.exit(1)

    # Build the label map
    labels = {s["id"]: s["label"] for s in stages}

    # Validate role assignments: every ID in STAGE_ROLES must exist
    role_ids = []
    role_ids.extend(STAGE_ROLES["active_stage_ids"])
    for k in ("in_lab_stage_id", "awaiting_sw_stage_id",
              "awaiting_activation_stage_id", "awaiting_transactions_stage_id",
              "onboarding_stage_id"):
        role_ids.append(STAGE_ROLES[k])

    missing = [sid for sid in role_ids if sid not in labels]
    if missing:
        print("=" * 60, file=sys.stderr)
        print("ERROR: Some role-assigned stage IDs are missing from the live "
              "HubSpot pipeline.", file=sys.stderr)
        print("This means the report would silently report 0 for the KPIs "
              "tied to these stages.", file=sys.stderr)
        print(f"Missing IDs: {missing}", file=sys.stderr)
        print(f"\nLive stage IDs in pipeline '{PIPELINE_ID}':",
              file=sys.stderr)
        for sid, lbl in labels.items():
            print(f"  {sid:25s} → {lbl}", file=sys.stderr)
        print("\nFix: update STAGE_ROLES in fetch_from_hubspot.py to "
              "reference current stage IDs.", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)

    return labels


def fetch_all_deals(token):
    """Page through the Deal search API to pull every deal in the pipeline."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    all_deals = []
    after = None

    while True:
        body = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "pipeline",
                    "operator": "EQ",
                    "value": PIPELINE_ID,
                }],
            }],
            "properties": DEAL_PROPERTIES,
            "limit": PAGE_SIZE,
        }
        if after:
            body["after"] = after

        data = hs_post(DEAL_SEARCH_URL, body, headers, label="deals")

        for r in data.get("results", []):
            props = r.get("properties", {})
            deal = {"id": int(r["id"])}
            for p in DEAL_PROPERTIES:
                deal[p] = props.get(p)
            all_deals.append(deal)

        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break

    return all_deals


def fetch_all_stores(token):
    """Page through the custom-object search API for Stores.

    No filter is applied here — we want every store record, including test
    data and inactive ones, so the report can choose how to bucket them.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    all_stores = []
    after = None

    while True:
        body = {
            "filterGroups": [],     # no filter = all records
            "properties": STORE_PROPERTIES,
            "limit": PAGE_SIZE,
        }
        if after:
            body["after"] = after

        data = hs_post(STORE_SEARCH_URL, body, headers, label="stores")
        # hs_post now raises after exhausting retries rather than returning
        # None — deliberately. A prior version caught failure here and
        # returned [] so "the deal data is still useful," but that silently
        # discarded every page already fetched and let every downstream
        # metric (Active Stores, Ready, Pending, Total Stores, pace,
        # funnel %) swap to a differently-defined number with no visible
        # indication anything was wrong. That's the mechanism behind the
        # 2026-07-01 incident's ~1,600-store phantom swing. Store data is
        # now all-or-nothing: either every page comes back, or the whole
        # pipeline run fails loudly and no report is generated for today.

        for r in data.get("results", []):
            props = r.get("properties", {})
            store = {"id": r["id"]}     # custom-object IDs can be very large; keep as string
            for p in STORE_PROPERTIES:
                store[p] = props.get(p)
            all_stores.append(store)

        paging = data.get("paging", {}).get("next", {})
        after = paging.get("after")
        if not after:
            break

    return all_stores


def summarize_stores(stores):
    """Return a quick {status: count} summary for stdout reporting."""
    by_status = {}
    test_count = 0
    for s in stores:
        if (s.get("is_test_data") or "").lower() == "true":
            test_count += 1
            continue
        status = s.get("status") or "(no status)"
        by_status[status] = by_status.get(status, 0) + 1
    return by_status, test_count


def write_pull(deals, stores, stage_labels, out_path, report_date=None):
    """Wrap deals + stores into the pull payload.

    Schema notes:
      - 'deals' (existing) is unchanged in shape, so generate_report.py keeps
        working without modification for everything that's already deal-based.
      - 'stage_labels' is now pulled live from HubSpot's Pipelines API.
        The role-assignments (which IDs are 'In Lab', 'Awaiting SW', etc.)
        come from STAGE_ROLES below; their existence is validated against
        the live stage list — if any role-ID disappears, the fetch fails
        loudly rather than silently zeroing the corresponding KPI.
      - 'stores' is the Store custom object data (operational truth).
    """
    payload = {
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_date": report_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "pipeline": PIPELINE_ID,
        "pipeline_label": "Retailer Activations",
        "stage_labels": stage_labels,                  # ← dynamic, from API
        "active_stage_ids":              STAGE_ROLES["active_stage_ids"],
        "in_lab_stage_id":               STAGE_ROLES["in_lab_stage_id"],
        "awaiting_sw_stage_id":          STAGE_ROLES["awaiting_sw_stage_id"],
        "awaiting_activation_stage_id":  STAGE_ROLES["awaiting_activation_stage_id"],
        "awaiting_transactions_stage_id":STAGE_ROLES["awaiting_transactions_stage_id"],
        "onboarding_stage_id":           STAGE_ROLES["onboarding_stage_id"],
        "deals": deals,
        "store_object_type_id": STORE_OBJECT_TYPE,
        "stores": stores,
    }
    Path(out_path).write_text(json.dumps(payload, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Fetch HubSpot data (deals + stores) for the TruAge Activation Report")
    parser.add_argument("--output", default="hubspot_pull.json",
                        help="Output path (default: hubspot_pull.json)")
    parser.add_argument("--report-date", default=None,
                        help="Override report 'as of' date (YYYY-MM-DD); default = today")
    parser.add_argument("--token", default=None,
                        help="HubSpot private app token (or set HUBSPOT_TOKEN env var)")
    parser.add_argument("--skip-stores", action="store_true",
                        help="Skip the Stores fetch (deal-only mode)")
    args = parser.parse_args()

    token = args.token or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        print("ERROR: No token. Set HUBSPOT_TOKEN env var or pass --token.",
              file=sys.stderr)
        print('  export HUBSPOT_TOKEN="pat-na1-..."', file=sys.stderr)
        sys.exit(1)

    print(f"Fetching pipeline stage labels for '{PIPELINE_ID}'...",
          file=sys.stderr)
    stage_labels = fetch_stage_labels(token)
    print(f"  → {len(stage_labels)} stages found and role IDs validated.",
          file=sys.stderr)

    print(f"Fetching deals from pipeline '{PIPELINE_ID}'...", file=sys.stderr)
    deals = fetch_all_deals(token)
    print(f"  → Retrieved {len(deals)} deals.", file=sys.stderr)

    if args.skip_stores:
        print("Skipping stores (--skip-stores).", file=sys.stderr)
        stores = []
    else:
        print(f"Fetching stores from custom object '{STORE_OBJECT_TYPE}'...",
              file=sys.stderr)
        stores = fetch_all_stores(token)
        print(f"  → Retrieved {len(stores)} stores.", file=sys.stderr)

        if stores:
            by_status, test_count = summarize_stores(stores)
            print(f"  → Status breakdown (excluding {test_count} test records):",
                  file=sys.stderr)
            for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
                print(f"      {status:20s}  {count:>5d}", file=sys.stderr)

    write_pull(deals, stores, stage_labels, args.output, args.report_date)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
