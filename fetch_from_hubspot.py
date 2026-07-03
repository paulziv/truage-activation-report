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
import sys
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# CONFIG — Retailer Activations pipeline (Deals)
# ============================================================
from truage_core import config
from truage_core import testrecords as _tr
from truage_core.hubspot import get_client, pull
PIPELINE_ID = config.PIPELINE_ID

# STAGE ROLES — the only stage IDs the report semantically depends on.
# Labels for ALL stages are pulled dynamically from HubSpot at fetch time
# (so renames absorb automatically). But the report needs to know which
# specific stages mean "Active", "In Lab", "Awaiting SW", etc. — those
# semantic assignments live here. If any of these IDs disappears from the
# pipeline, the fetch fails loudly so we don't silently zero out a KPI.
STAGE_ROLES = config.STAGE_ROLES

DEAL_PROPERTIES = config.DEAL_PROPERTIES

# ============================================================
# CONFIG — Stores custom object
# ============================================================
# Object type ID for the custom Stores object in HubSpot.
# Find this in HubSpot: Settings → Objects → Custom Objects → click Stores
# (or it's in the URL of the property settings page: type=2-XXXXXXXX).
STORE_OBJECT_TYPE = config.STORE_OBJECT_TYPE

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
STORE_PROPERTIES = config.STORE_PROPERTIES

# HTTP + fetch layer now lives in truage_core.hubspot (client + pull); see main().


def summarize_stores(stores):
    """Return a quick {status: count} summary for stdout reporting."""
    by_status = {}
    test_count = 0
    for s in stores:
        if _tr.is_test_store(s):
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

    token = args.token or os.environ.get("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if not token:
        print("ERROR: No token. Set HUBSPOT_TOKEN env var or pass --token.",
              file=sys.stderr)
        print('  export HUBSPOT_TOKEN="pat-na1-..."', file=sys.stderr)
        sys.exit(1)

    client = get_client(token)

    print(f"Fetching pipeline stage labels for '{PIPELINE_ID}'...",
          file=sys.stderr)
    stage_labels = pull.fetch_stage_labels(client)
    print(f"  → {len(stage_labels)} stages found and role IDs validated.",
          file=sys.stderr)

    print(f"Fetching deals from pipeline '{PIPELINE_ID}'...", file=sys.stderr)
    deals = pull.fetch_all_deals(client)
    print(f"  → Retrieved {len(deals)} deals.", file=sys.stderr)

    if args.skip_stores:
        print("Skipping stores (--skip-stores).", file=sys.stderr)
        stores = []
    else:
        print(f"Fetching stores from custom object '{STORE_OBJECT_TYPE}'...",
              file=sys.stderr)
        stores = pull.fetch_all_stores(client)
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
