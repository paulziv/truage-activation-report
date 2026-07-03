#!/usr/bin/env bash
# apply_truage_activation_incident_fix.sh
#
# Fixes the root cause of the 2026-07-01 incident: a ~1,600-store phantom
# swing in "Active Stores" plus empty Ready/Pending, triggered by HubSpot
# rate-limiting during a shared cron trigger.
#
# What was actually happening:
#   1. fetch_from_hubspot.py had NO retry/backoff at all — a single 429 or
#      5xx failed the request immediately.
#   2. fetch_all_stores() discarded every already-fetched page on any
#      single page failure, returning [] instead of partial data or an error.
#   3. generate_report_html.py silently swapped "Active Stores" between two
#      structurally different numbers (a Store-status count vs. a deal-
#      amount sum) depending on whether that day's Store fetch succeeded —
#      and those two numbers disagree by design, producing a fake "swing"
#      any time the Store fetch failed, even briefly.
#
# Decision (confirmed): never show false, estimated, or stale numbers.
# Better to fail the run and alert than publish a misleading report.
#
# This script:
#   - Adds real retry/backoff (respecting Retry-After) to hs_post/hs_get.
#   - Makes fetch_all_stores fail loud after retries are exhausted,
#     instead of silently returning [].
#   - Makes generate_report_html.py refuse to render (exit 1) if store
#     data is missing, instead of substituting a different metric.
#   - Fixes the Total Stores KPI cell to show "—" instead of "0" when
#     store data is unavailable (defense in depth; should be unreachable
#     in production now that the above two changes are in place).
#
# This does NOT by itself stop yesterday's report from being re-served —
# see the companion apply_pez_portal_freshness_fix.sh for that half.
#
# USAGE:
#   Run this from the root of your truage-activation-report checkout:
#     bash apply_truage_activation_incident_fix.sh
#
# Does not commit or push — review `git diff` and commit on your own schedule.

set -euo pipefail

if [[ ! -f "fetch_from_hubspot.py" || ! -f "generate_report_html.py" ]]; then
  echo "ERROR: fetch_from_hubspot.py and/or generate_report_html.py not found."
  echo "Run this script from the root of your truage-activation-report checkout."
  exit 1
fi

if ! grep -q 'def hs_post' fetch_from_hubspot.py || grep -q '_request_with_retry' fetch_from_hubspot.py; then
  echo "NOTE: fetch_from_hubspot.py doesn't look like the expected pre-fix state"
  echo "(either already patched, or has changed since this script was written)."
  echo "Skipping to avoid a bad patch application. Check manually if unsure."
  exit 0
fi

PATCH_FILE="$(mktemp)"
trap 'rm -f "$PATCH_FILE"' EXIT

cat > "$PATCH_FILE" << 'PATCH_EOF'
diff --git a/fetch_from_hubspot.py b/fetch_from_hubspot.py
index 03fc1c2..ea36aca 100644
--- a/fetch_from_hubspot.py
+++ b/fetch_from_hubspot.py
@@ -33,7 +33,9 @@ Token scopes needed:
 import argparse
 import json
 import os
+import random
 import sys
+import time
 from datetime import datetime, timezone
 from pathlib import Path
 
@@ -127,26 +129,83 @@ STORE_SEARCH_URL = f"https://api.hubapi.com/crm/v3/objects/{STORE_OBJECT_TYPE}/s
 PAGE_SIZE = 200
 
 
-def hs_post(url, body, headers, *, label=""):
-    """POST to HubSpot. On error, print HTTP body and return None."""
-    resp = requests.post(url, json=body, headers=headers, timeout=30)
-    if resp.status_code != 200:
-        print(f"ERROR ({label}): HubSpot API returned {resp.status_code}",
+MAX_RETRIES = 5
+BASE_BACKOFF_SECONDS = 1.0
+
+
+def _request_with_retry(method, url, headers, *, json_body=None, label=""):
+    """POST/GET to HubSpot with retry + exponential backoff.
+
+    Handles the failure modes that caused the 2026-07-01 incident (a burst
+    of 429s during a shared cron trigger silently produced an empty Stores
+    result, which downstream got treated as valid data). This function
+    NEVER returns None on failure — after exhausting MAX_RETRIES it raises,
+    so callers fail loudly instead of silently degrading. That's
+    deliberate: a report built from partial data is worse than no report,
+    since it can misrepresent real numbers without any visible indication.
+
+    - 429: respects the Retry-After header when HubSpot sends one,
+      otherwise falls back to exponential backoff.
+    - 5xx / network errors: exponential backoff with jitter.
+    - other 4xx: non-retryable, raises immediately (retrying won't help).
+    """
+    last_exc = None
+    for attempt in range(1, MAX_RETRIES + 1):
+        try:
+            if method == "POST":
+                resp = requests.post(url, json=json_body, headers=headers, timeout=30)
+            else:
+                resp = requests.get(url, headers=headers, timeout=30)
+        except requests.RequestException as exc:
+            last_exc = exc
+            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
+            print(f"WARNING ({label}): network error on attempt {attempt}/{MAX_RETRIES}: {exc}. "
+                  f"Retrying in {wait:.1f}s.", file=sys.stderr)
+            time.sleep(wait)
+            continue
+
+        if resp.status_code == 200:
+            return resp.json()
+
+        if resp.status_code == 429:
+            retry_after = resp.headers.get("Retry-After")
+            wait = float(retry_after) if retry_after else BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
+            print(f"WARNING ({label}): 429 rate-limited on attempt {attempt}/{MAX_RETRIES} "
+                  f"(Retry-After={retry_after!r}). Waiting {wait:.1f}s.", file=sys.stderr)
+            time.sleep(wait)
+            continue
+
+        if 500 <= resp.status_code < 600:
+            wait = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
+            print(f"WARNING ({label}): HubSpot returned {resp.status_code} on attempt "
+                  f"{attempt}/{MAX_RETRIES}. Retrying in {wait:.1f}s.", file=sys.stderr)
+            print(resp.text, file=sys.stderr)
+            time.sleep(wait)
+            continue
+
+        # Non-retryable client error (400, 401, 403, 404, etc.) — retrying
+        # won't fix a bad token or a malformed request. Fail immediately.
+        print(f"ERROR ({label}): HubSpot API returned {resp.status_code} (non-retryable).",
               file=sys.stderr)
         print(resp.text, file=sys.stderr)
-        return None
-    return resp.json()
+        raise RuntimeError(f"{label}: HubSpot API returned {resp.status_code} (non-retryable)")
+
+    raise RuntimeError(
+        f"{label}: exhausted {MAX_RETRIES} retries against HubSpot API"
+        + (f" (last error: {last_exc})" if last_exc else "")
+    )
+
+
+def hs_post(url, body, headers, *, label=""):
+    """POST to HubSpot with retry/backoff. Raises on failure — never
+    returns None. See _request_with_retry for retry policy."""
+    return _request_with_retry("POST", url, headers, json_body=body, label=label)
 
 
 def hs_get(url, headers, *, label=""):
-    """GET from HubSpot. On error, print HTTP body and return None."""
-    resp = requests.get(url, headers=headers, timeout=30)
-    if resp.status_code != 200:
-        print(f"ERROR ({label}): HubSpot API returned {resp.status_code}",
-              file=sys.stderr)
-        print(resp.text, file=sys.stderr)
-        return None
-    return resp.json()
+    """GET from HubSpot with retry/backoff. Raises on failure — never
+    returns None. See _request_with_retry for retry policy."""
+    return _request_with_retry("GET", url, headers, label=label)
 
 
 def fetch_stage_labels(token):
@@ -163,8 +222,6 @@ def fetch_stage_labels(token):
     }
     url = f"https://api.hubapi.com/crm/v3/pipelines/deals/{PIPELINE_ID}"
     data = hs_get(url, headers, label="pipeline-stages")
-    if data is None:
-        sys.exit(1)
 
     stages = data.get("stages", [])
     if not stages:
@@ -228,8 +285,6 @@ def fetch_all_deals(token):
             body["after"] = after
 
         data = hs_post(DEAL_SEARCH_URL, body, headers, label="deals")
-        if data is None:
-            sys.exit(1)
 
         for r in data.get("results", []):
             props = r.get("properties", {})
@@ -269,12 +324,16 @@ def fetch_all_stores(token):
             body["after"] = after
 
         data = hs_post(STORE_SEARCH_URL, body, headers, label="stores")
-        if data is None:
-            # Stores fetch failure shouldn't kill the whole run — the deal data
-            # is still useful. Print the warning and continue with an empty list.
-            print("WARNING: store fetch failed — continuing with deals only.",
-                  file=sys.stderr)
-            return []
+        # hs_post now raises after exhausting retries rather than returning
+        # None — deliberately. A prior version caught failure here and
+        # returned [] so "the deal data is still useful," but that silently
+        # discarded every page already fetched and let every downstream
+        # metric (Active Stores, Ready, Pending, Total Stores, pace,
+        # funnel %) swap to a differently-defined number with no visible
+        # indication anything was wrong. That's the mechanism behind the
+        # 2026-07-01 incident's ~1,600-store phantom swing. Store data is
+        # now all-or-nothing: either every page comes back, or the whole
+        # pipeline run fails loudly and no report is generated for today.
 
         for r in data.get("results", []):
             props = r.get("properties", {})
diff --git a/generate_report_html.py b/generate_report_html.py
index 4f4677f..ef78ffa 100644
--- a/generate_report_html.py
+++ b/generate_report_html.py
@@ -29,6 +29,7 @@ import json
 import math
 import re
 import statistics
+import sys
 from collections import defaultdict
 from datetime import datetime, timedelta, timezone
 from pathlib import Path
@@ -1134,6 +1135,7 @@ def _page1(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A, b):
     star_sw     = "*" if any(d.is_test_record() for d in M["by_stage"].get(M["sw_id"], [])) else ""
     kpi_ready   = M["ready_stores_real"]   if M["has_store_data"] else "—"
     kpi_pending = M["pending_stores_real"] if M["has_store_data"] else "—"
+    kpi_total   = M["stores_total_real"]   if M["has_store_data"] else "—"
     fwd_count   = len(M["fwd_calendar_top5"])
 
     def _fmt_or_dash(value):
@@ -1309,7 +1311,7 @@ def _page1(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A, b):
         _kpi("Active Stores",     f"{active:,}{h(star_active)}", act_sub,          act_cls,  act_arrow),
         _kpi("Ready",             _fmt_or_dash(kpi_ready),       "onboarded · not transacting",          rdy_cls,  rdy_arrow),
         _kpi("Pending",           _fmt_or_dash(kpi_pending),     "contracts complete · not onboarded",   pnd_cls,  pnd_arrow),
-        _kpi("Total Stores",      f"{M['stores_total_real']:,}", "all status buckets · ex-test",         tot_cls,  tot_arrow),
+        _kpi("Total Stores",      _fmt_or_dash(kpi_total),       "all status buckets · ex-test",         tot_cls,  tot_arrow),
         _kpi("Awaiting Software", f"{M['sw_stores']:,}{h(star_sw)}", h(sw_sub),    sw_cls,   sw_arrow),
         _kpi("Fwd Calendar (14d)",str(fwd_count),                f"target {fwd_target}+",                fwd_cls,  fwd_arrow),
     ])
@@ -2231,6 +2233,30 @@ def main():
         f"stale-early={len(a['stale_early_funnel'])}"
     )
 
+    # Store data is required, full stop. Earlier behavior substituted a
+    # deal-amount sum for the headline "Active Stores" number (and pace,
+    # weekly delta, projected total, funnel %) whenever the Stores fetch
+    # came back empty, with Ready/Pending falling back to "—". Those two
+    # source numbers disagree by design (deal amounts lag real activation
+    # status) — silently swapping between them is what produced the
+    # ~1,600-store phantom swing in the 2026-07-01 incident. Per decision:
+    # never show false, estimated, or stale numbers. If store data isn't
+    # available, refuse to generate a report at all rather than render one
+    # with numbers that don't mean what their labels say — fail loudly so
+    # the run gets flagged and re-fetched, instead of quietly shipping a
+    # misleading report to stakeholders.
+    if not M["has_store_data"]:
+        print(
+            "ERROR: No Store data available (HubSpot custom-object fetch "
+            "returned no records). Refusing to generate a report — doing so "
+            "would either show '—' for Ready/Pending/Total Stores or, worse, "
+            "silently substitute a deal-amount sum for the Active Stores "
+            "count under the same label. Re-run once the Stores fetch is "
+            "confirmed working.",
+            file=sys.stderr,
+        )
+        sys.exit(1)
+
     pulled_at_str = (parse_dt(payload.get("pulled_at")) or asof).strftime("%Y-%m-%d %H:%M UTC")
     print(f"Rendering HTML…")
     html = render_html(M, pulled_at_str, asof)
PATCH_EOF

echo "Applying patch to fetch_from_hubspot.py and generate_report_html.py..."
git apply --whitespace=nowarn "$PATCH_FILE"
echo "Patch applied."

echo "Verifying both files compile..."
python3 -m py_compile fetch_from_hubspot.py generate_report_html.py
echo "Compiles OK."

echo ""
echo "Done. Review with: git diff fetch_from_hubspot.py generate_report_html.py"
echo "This script did not commit or push — that's on you."
echo ""
echo "IMPORTANT: this is one half of the full fix. The other half — making"
echo "sure pez-portal never caches/emails a stale report when this pipeline"
echo "fails — is in apply_pez_portal_freshness_fix.sh. Apply both."
