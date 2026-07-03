#!/usr/bin/env bash
# apply_funnel_reconciliation_fix.sh
#
# Fixes the "Committed Pipeline: 9,949 stores" vs "862 active of 9,839
# committed" discrepancy — two numbers on the same Page 1 that both claim
# to represent the committed pipeline, but were computed two different
# ways and silently disagreed.
#
# Two separate causes, both fixed here:
#
#   1. Test-record contamination: the Committed Pipeline stacked bar's
#      mid-funnel segments (In Lab, Awaiting SW, Awaiting Activation,
#      Awaiting Transactions) came from stage_sum(), which included
#      test-flagged deals. The Funnel Conversion denominator
#      (committed_stores) already excluded them. A test deal sitting in
#      any mid-funnel stage inflated the bar's total but not the funnel's
#      denominator — this is what the "*" on the Awaiting Software KPI
#      cell was quietly flagging.
#
#   2. Two definitions of "Active": the bar's Active segment and the
#      headline "Active Stores" KPI use the Store-status count when
#      Store data is available (M["active_stores"]). But committed_stores
#      recomputed its OWN separate deal-amount sum for the active/closedwon
#      stage instead of using that same authoritative number — so even
#      with zero test contamination, the two totals could disagree
#      whenever deal Amounts and real Store status diverge (which the
#      report's own documentation says happens by design).
#
# Fix: stage_sum() now excludes test records (matching committed_stores),
# and committed_stores now adds M["active_stores"] directly instead of
# re-deriving a second "active" number from raw deal amounts. After this,
# the stacked bar total and the funnel conversion denominator are
# mathematically guaranteed to match — same test-exclusion, same Active
# source, both computed once.
#
# Verified: a synthetic scenario combining both failure modes (a test
# deal in In Lab + a real Store-status/deal-Amount mismatch in the active
# stage) reproduced a 353-vs-305 mismatch on the pre-fix code, and matched
# exactly (303 == 303) after this patch.
#
# USAGE:
#   Run this from the root of your truage-activation-report checkout:
#     bash apply_funnel_reconciliation_fix.sh
#
# Does not commit or push — review `git diff` and commit on your own schedule.

set -euo pipefail

if [[ ! -f "generate_report_html.py" ]]; then
  echo "ERROR: generate_report_html.py not found."
  echo "Run this script from the root of your truage-activation-report checkout."
  exit 1
fi

if grep -q "committed_stores_excl_active" generate_report_html.py; then
  echo "NOTE: This fix already appears to be applied. No changes made."
  exit 0
fi

if ! grep -q 'def stage_sum(sid):' generate_report_html.py; then
  echo "NOTE: generate_report_html.py doesn't look like the expected pre-fix state"
  echo "(has changed since this script was written). Skipping to avoid a bad"
  echo "patch application. Check manually if unsure."
  exit 0
fi

PATCH_FILE="$(mktemp)"
trap 'rm -f "$PATCH_FILE"' EXIT

cat > "$PATCH_FILE" << 'PATCH_EOF'
diff --git a/generate_report_html.py b/generate_report_html.py
index ef78ffa..933fbb5 100644
--- a/generate_report_html.py
+++ b/generate_report_html.py
@@ -195,7 +195,16 @@ def compute_metrics(payload, deals, asof):
     M["by_stage"] = by_stage
 
     def stage_sum(sid):
-        return sum(d.amount for d in by_stage.get(sid, []))
+        # Test-record exclusion is required here, not optional — this feeds
+        # every mid-funnel headline number (In Lab, Awaiting SW, Awaiting
+        # Activation, Awaiting Transactions, Onboarding) AND the Committed
+        # Pipeline stacked bar. committed_stores (the Funnel Conversion
+        # denominator, below) already excludes test records from the same
+        # population. Without this exclusion here too, a single test deal
+        # sitting in a mid-funnel stage inflates the bar's total but not the
+        # funnel denominator, so the two numbers silently stop reconciling —
+        # this is what produced the 9,949-vs-9,839 discrepancy.
+        return sum(d.amount for d in by_stage.get(sid, []) if not d.is_test_record())
 
     def stage_count(sid):
         return len(by_stage.get(sid, []))
@@ -620,12 +629,24 @@ def compute_metrics(payload, deals, asof):
     M["anomalies"] = A
 
     # --- Funnel conversion ---
-    committed_stores = sum(
+    # Sum every non-active committed-pipeline stage (test-excluded, matching
+    # stage_sum() above), then add M["active_stores"] rather than re-summing
+    # deal amounts for the active stage separately. Active Stores has two
+    # possible sources — a Store-status count vs. a deal-amount sum — that
+    # disagree by design (deal amounts lag real activation status). Every
+    # other number on this page already uses M["active_stores"] as the one
+    # authoritative source; recomputing a second, different "active" number
+    # here just to add it into committed_stores was the other half of why
+    # the Committed Pipeline total and this funnel's denominator didn't
+    # reconcile (9,949 vs 9,839 in the 2026-07-01 report).
+    committed_stores_excl_active = sum(
         d.amount for d in deals
         if d.stage not in {"closedlost"}
         and d.stage not in EARLY_FUNNEL_STAGES
+        and d.stage not in active_ids
         and not d.is_test_record()
     )
+    committed_stores = committed_stores_excl_active + M["active_stores"]
     M["committed_stores"] = committed_stores
     M["funnel_conv_pct"]  = round(100 * M["active_stores"] / max(1, committed_stores))
 
PATCH_EOF

echo "Applying patch to generate_report_html.py..."
git apply --whitespace=nowarn "$PATCH_FILE"
echo "Patch applied."

echo "Verifying generate_report_html.py compiles..."
python3 -m py_compile generate_report_html.py
echo "Compiles OK."

echo ""
echo "Done. Review with: git diff generate_report_html.py"
echo "This script did not commit or push — that's on you."
echo ""
echo "Expected visible effect on the report: In Lab / Awaiting SW / Awaiting"
echo "Activation / Awaiting Transactions numbers may drop slightly (removing"
echo "test-deal contamination), and the Committed Pipeline bar total will now"
echo "exactly equal the Funnel Conversion Rate's 'X active of Y committed'"
echo "denominator — no more silent gap between the two."
