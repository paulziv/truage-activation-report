#!/usr/bin/env bash
# apply_pagination_pacing_delay.sh
#
# Small complementary hardening on top of apply_truage_activation_incident_fix.sh.
#
# That fix added reactive retry/backoff for 429s — it waits and retries
# *after* HubSpot's per-second rate limit is hit. This adds a small
# proactive pacing delay (150ms) between pages during Deals/Stores
# pagination, so the pipeline is less likely to hit that per-second cap
# in the first place. Cheap, low-risk, purely additive.
#
# Confirmed root cause from HubSpot's own API call log for the 2026-07-01
# incident: a 429 ("You have reached your secondly limit") hit on page 2
# of a multi-page Stores pull, ~180ms after page 1 succeeded — i.e. two
# back-to-back page requests with no gap between them. This delay directly
# targets that failure mode.
#
# REQUIRES apply_truage_activation_incident_fix.sh to already be applied —
# this patches the pagination loops that fix introduced/touched.
#
# USAGE:
#   Run this from the root of your truage-activation-report checkout,
#   after applying apply_truage_activation_incident_fix.sh:
#     bash apply_pagination_pacing_delay.sh
#
# Does not commit or push — review `git diff` and commit on your own schedule.

set -euo pipefail

if [[ ! -f "fetch_from_hubspot.py" ]]; then
  echo "ERROR: fetch_from_hubspot.py not found."
  echo "Run this script from the root of your truage-activation-report checkout."
  exit 1
fi

if ! grep -q '_request_with_retry' fetch_from_hubspot.py; then
  echo "ERROR: fetch_from_hubspot.py doesn't have the retry/backoff fix yet."
  echo "Apply apply_truage_activation_incident_fix.sh first — this script"
  echo "patches pagination loops that fix introduced."
  exit 1
fi

if grep -q 'Small pacing delay between pages' fetch_from_hubspot.py; then
  echo "NOTE: This pacing delay already appears to be applied. No changes made."
  exit 0
fi

PATCH_FILE="$(mktemp)"
trap 'rm -f "$PATCH_FILE"' EXIT

cat > "$PATCH_FILE" << 'PATCH_EOF'
diff --git a/fetch_from_hubspot.py b/fetch_from_hubspot.py
index ea36aca..4c0a694 100644
--- a/fetch_from_hubspot.py
+++ b/fetch_from_hubspot.py
@@ -297,6 +297,10 @@ def fetch_all_deals(token):
         after = paging.get("after")
         if not after:
             break
+        # Small pacing delay between pages — spreads requests out so we're
+        # less likely to hit HubSpot's per-second rate limit in the first
+        # place, complementing the reactive retry/backoff in hs_post.
+        time.sleep(0.15)
 
     return all_deals
 
@@ -346,6 +350,7 @@ def fetch_all_stores(token):
         after = paging.get("after")
         if not after:
             break
+        time.sleep(0.15)  # see rationale in fetch_all_deals
 
     return all_stores
 
PATCH_EOF

echo "Applying patch to fetch_from_hubspot.py..."
git apply --whitespace=nowarn "$PATCH_FILE"
echo "Patch applied."

echo "Verifying fetch_from_hubspot.py compiles..."
python3 -m py_compile fetch_from_hubspot.py
echo "Compiles OK."

echo ""
echo "Done. Review with: git diff fetch_from_hubspot.py"
echo "This script did not commit or push — that's on you."
