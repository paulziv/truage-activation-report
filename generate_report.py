"""
TruAge Activation Report — Generator
=====================================

Reads:  hubspot_pull.json
Writes: TruAge_Activation_Report_<DATE>.pdf

PRINCIPLES (do not violate):
  1. The 25,000 store goal by Dec 31, 2026 is the ONLY hardcoded number.
     Every other figure on the report is computed from the JSON pull.
  2. Tone is forthcoming, not accusatory. No exclamation points, no
     urgency theatrics. State what is, what's next, what's missing.
  3. Page 3 is a complete punch list — every data quality issue is
     surfaced with deal name(s), the issue, and the action so the data
     team can fix records before the next pull.

To run:
    python generate_report.py                      # uses today
    python generate_report.py --date 2026-05-06    # specific week
    python generate_report.py --input mypull.json --output myreport.pdf
"""
from __future__ import annotations
import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle
import matplotlib.pyplot as plt

# ============================================================
# CONFIG — the only hardcoded numbers in this report
# ============================================================
GOAL = 25000
GOAL_DATE_STR = "Dec 31, 2026"
GOAL_DATE = datetime(2026, 12, 31, tzinfo=timezone.utc)

# Test/junk record patterns. Conservative on purpose.
TEST_SUBSTRING_PATTERNS = [
    "thinksys",
    "qrjwxjqsbuxciwmljofcd",
    "demo unit",
    "homeless not helpless",
    "muhammad hassan",
    "mendietaaaa",
    "bunny palace",
]
TEST_EXACT_NAMES = {
    "tester", "self employed", "send proud", "rita", "pan", "na",
    "clover",
}

# Vendor recognition patterns from `blocked_reason` field
VENDOR_PATTERNS = {
    "Verifone (Commander)": ["verifone", "commander"],
    "NCR (Radiant)": ["ncr", "radiant"],
    "Invenco": ["invenco"],
    "Gilbarco": ["gilbarco"],
}

# Stages we treat as "early funnel" — top-of-funnel prospecting work
# that should not appear on Page 1 stalled-deal table.
EARLY_FUNNEL_STAGES = {
    "1346410815",            # Leads
    "1350980982",            # Unqualified
    "qualifiedtobuy",        # Qualified
    "appointmentscheduled",  # Get Started Form Received
    "presentationscheduled", # Legals Signed
    "decisionmakerboughtin", # Setup Guide Sent
    "1335845536",            # Parking Lot 3 Other
}

# Layout tokens (locked)
# ============================================================
# COLOR PALETTE — Convenience.org branding
# ============================================================
# Primary: navy. Positive emphasis: green-teal. Alert/needs-attention: coral.
# Body text stays near-black. Cream and pink fill the callout bands.
INK         = HexColor("#1a1a1a")  # body text
INK_SOFT    = HexColor("#4a4a4a")  # secondary body text
INK_FAINT   = HexColor("#888888")  # captions, footers
RULE        = HexColor("#cccccc")  # standard rule
RULE_SOFT   = HexColor("#e5e5e5")  # soft rule
# Brand primary
NAVY        = HexColor("#1F3864")  # titles, table headers, KPI numbers, section labels
NAVY_DARK   = HexColor("#16294A")  # navy emphasis (rare)
NAVY_TEXT   = HexColor("#ffffff")  # text on navy fill
# Brand positive
GOOD        = HexColor("#0F6E56")  # green-teal: closed-won bullets, positive deltas
GOOD_SOFT   = HexColor("#2A9577")  # lighter green-teal for chart fills
# Brand alert (coral) — needs attention, not just decoration
ACCENT      = HexColor("#C04828")  # alert text, "Required next week" markers
ACCENT_DARK = HexColor("#8B2418")  # alert emphasis (deep coral)
# Band backgrounds
BG_BAND     = HexColor("#FEF4E8")  # cream — trajectory band
BG_KPI      = HexColor("#FAFAF7")  # near-white — KPI cells
BG_STALLED  = HexColor("#FBECEA")  # pink — stalled/alert band
# Page 3 anomaly intro callout (light navy tint, kept distinct)
BG_INFO     = HexColor("#EEF2FB")
BORDER_INFO = HexColor("#3B58A8")
# Header underline (navy)
HEADER_RULE = NAVY

PAGE_W, PAGE_H = letter
MARGIN_L = 0.5 * inch
MARGIN_R = 0.5 * inch
MARGIN_T = 0.5 * inch
MARGIN_B = 0.5 * inch
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R

# ============================================================
# DATA LOADING
# ============================================================
def parse_dt(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fmt_date(dt, pattern):
    """Cross-platform strftime that strips leading zeros from day/month.

    Python's strftime supports '%-d' on Linux/macOS to drop leading zeros
    on the day number, but Windows uses '%#d' instead. Rather than fork the
    format strings per OS, this helper uses padded formats ('%d', '%m') and
    strips the resulting leading zeros post-hoc. Works everywhere.

    Example:  fmt_date(asof, "%b %-d, %Y") → "May 8, 2026" on any OS.
    """
    pat = pattern.replace("%-d", "%d").replace("%-m", "%m")
    s = dt.strftime(pat)
    # Strip leading-zero day/month after a space (e.g. "May 08" → "May 8")
    # but not the year (which can legitimately have a leading zero historically
    # — though not in any realistic case for this report).
    import re
    # Replace " 0X" with " X" where X is a digit, but only for day/month
    # positions. Conservative: just collapse any " 0\d" to " \d".
    s = re.sub(r' 0(\d)', r' \1', s)
    # Also handle start-of-string case ("08:30 AM" type — unlikely here, but safe)
    s = re.sub(r'^0(\d)', r'\1', s)
    return s

def safe_int(v):
    if v in (None, "", "null"):
        return 0
    try:
        return int(float(v))
    except Exception:
        return 0

class Deal:
    def __init__(self, raw):
        self.id = raw.get("id")
        self.name = (raw.get("dealname") or "").strip()
        self.stage = raw.get("dealstage") or ""
        self.amount = safe_int(raw.get("amount"))
        self.amount_raw = raw.get("amount")
        self.owner = raw.get("hubspot_owner_id") or ""
        self.blocked_reason = (raw.get("blocked_reason") or "").strip()
        self.created = parse_dt(raw.get("createdate"))
        self.closed = parse_dt(raw.get("closedate"))
        self.next_activity = parse_dt(raw.get("notes_next_activity_date"))
        self.entered_current = parse_dt(raw.get("hs_v2_date_entered_current_stage"))
        self.stage_entries = {
            k.replace("hs_v2_date_entered_", ""): parse_dt(v)
            for k, v in raw.items()
            if k.startswith("hs_v2_date_entered_") and v
        }

    def days_in_stage(self, asof):
        if not self.entered_current:
            return None
        return (asof - self.entered_current).days

    def is_test_record(self):
        n = self.name.strip().lower()
        if not n:
            return False
        normalized = n.split(" - new deal")[0].strip()
        if normalized in TEST_EXACT_NAMES:
            return True
        if any(p in n for p in TEST_SUBSTRING_PATTERNS):
            return True
        return False

    def amount_missing(self):
        return self.amount_raw in (None, "")

    def amount_zero_explicit(self):
        return self.amount_raw not in (None, "") and safe_int(self.amount_raw) == 0


def load_data(path):
    payload = json.loads(Path(path).read_text())
    deals = [Deal(d) for d in payload.get("deals", [])]
    return payload, deals


# ============================================================
# DERIVED METRICS
# ============================================================
def compute_metrics(payload, deals, asof):
    M = {}
    stage_labels = payload["stage_labels"]
    in_lab_id    = payload["in_lab_stage_id"]
    sw_id        = payload["awaiting_sw_stage_id"]
    act_id       = payload["awaiting_activation_stage_id"]
    trans_id     = payload["awaiting_transactions_stage_id"]
    onb_id       = payload["onboarding_stage_id"]
    active_ids   = set(payload["active_stage_ids"])

    # Expose stage IDs on M so downstream functions can target stages
    # without needing the payload.
    M["payload"] = payload
    M["stage_labels"] = stage_labels
    M["in_lab_id"]    = in_lab_id
    M["sw_id"]        = sw_id
    M["act_id"]       = act_id
    M["trans_id"]     = trans_id
    M["onb_id"]       = onb_id
    M["active_ids"]   = active_ids

    M["asof"] = asof
    M["asof_label"] = fmt_date(asof, "%A, %B %-d, %Y")
    M["pulled_at"] = payload.get("pulled_at", "")

    by_stage = defaultdict(list)
    for d in deals:
        by_stage[d.stage].append(d)
    M["by_stage"] = by_stage

    def stage_sum(sid):  return sum(d.amount for d in by_stage.get(sid, []))
    def stage_count(sid): return len(by_stage.get(sid, []))

    # ============================================================
    # DEAL-DERIVED store counts (sales pipeline view).
    # These come from summing the Amount field on deal records.
    # They lag actual store state because Amount fields aren't always
    # updated when stores activate — that gap is surfaced on Page 3.
    # ============================================================
    M["active_stores_deals"]   = sum(stage_sum(s) for s in active_ids)
    M["active_deal_count"]     = sum(stage_count(s) for s in active_ids)
    M["in_lab_stores"]         = stage_sum(in_lab_id)
    M["sw_stores"]             = stage_sum(sw_id)
    M["act_stores"]            = stage_sum(act_id)
    M["trans_stores"]          = stage_sum(trans_id)
    M["onb_stores"]            = stage_sum(onb_id)
    M["in_progress_stores"]    = (
        M["in_lab_stores"] + M["sw_stores"] + M["act_stores"] +
        M["trans_stores"] + M["onb_stores"]
    )

    # ============================================================
    # STORE-DERIVED counts (operational truth).
    # These come from the Stores custom object's `status` property.
    # Active = transacting, Ready = onboarded but not transacting,
    # Pending = contracts complete but not onboarded.
    # ============================================================
    stores = payload.get("stores", []) or []
    M["has_store_data"] = bool(stores)
    M["stores_total"]   = len(stores)

    # Filter test records out via the is_test_data field
    def is_test_store(s):
        v = s.get("is_test_data")
        if v is True or (isinstance(v, str) and v.lower() == "true"):
            return True
        return False

    real_stores = [s for s in stores if not is_test_store(s)]
    test_stores = [s for s in stores if is_test_store(s)]
    M["stores_test_count"] = len(test_stores)
    M["real_stores"]       = real_stores

    # Group by status — defensively, since values aren't documented
    by_status = defaultdict(list)
    for s in real_stores:
        status = (s.get("status") or "").strip()
        if not status:
            status = "(no status)"
        by_status[status].append(s)
    M["stores_by_status"] = {k: len(v) for k, v in by_status.items()}

    # Headline values — case-insensitive match against the documented values
    def status_count(name):
        for k, lst in by_status.items():
            if k.lower() == name.lower():
                return len(lst)
        return 0

    M["active_stores_real"]  = status_count("Active")
    M["pending_stores_real"] = status_count("Pending")
    M["ready_stores_real"]   = status_count("Ready")

    # The headline "Active Stores" KPI — uses store data when available,
    # falls back to deal-Amount sum if the pull predates the stores fetch.
    if M["has_store_data"]:
        M["active_stores"] = M["active_stores_real"]
    else:
        M["active_stores"] = M["active_stores_deals"]

    # Active delta (deal-based) — closed-won this week.
    # Kept for the "What moved this week" prose (it tells us which retailers
    # the sales team flipped, not just total store count).
    week_ago = asof - timedelta(days=7)
    moved_this_week_active = []
    for d in by_stage.get("closedwon", []):
        ts = d.stage_entries.get("closedwon") or d.closed
        if ts and week_ago <= ts <= asof:
            moved_this_week_active.append((d, ts))
    M["active_delta_week_deals"]      = sum(d.amount for d, _ in moved_this_week_active)
    M["active_delta_deal_count"] = len(moved_this_week_active)
    M["moved_this_week_active"] = sorted(moved_this_week_active, key=lambda x: x[1], reverse=True)

    # Active delta (store-based) — stores that activated this week.
    # This is what the headline KPI subtitle uses ("+N this week").
    stores_activated_this_week = 0
    if M["has_store_data"]:
        for s in real_stores:
            ts = parse_dt(s.get("activated_at"))
            if ts and week_ago <= ts <= asof:
                stores_activated_this_week += 1
    M["active_delta_week_stores"] = stores_activated_this_week

    # The canonical delta — uses store count when available, else deals.
    M["active_delta_week"] = (
        M["active_delta_week_stores"] if M["has_store_data"]
        else M["active_delta_week_deals"]
    )

    # Trajectory math
    months_left = max(1, (GOAL_DATE.year - asof.year) * 12 + (GOAL_DATE.month - asof.month))
    M["months_left"]   = months_left
    M["goal"]          = GOAL
    M["goal_date_str"] = GOAL_DATE_STR

    # Current pace — stores activated in the last 30 days.
    # We prefer the store-based count (real activation timestamps from the
    # Stores object) over deal-Amount sums, because Amount fields lag actual
    # store activations by weeks. When store data isn't in the pull, we fall
    # back to the deal-Amount path.
    month_ago = asof - timedelta(days=30)
    pace_deals = sum(
        d.amount for d in by_stage.get("closedwon", [])
        if (d.stage_entries.get("closedwon") or d.closed)
        and month_ago <= (d.stage_entries.get("closedwon") or d.closed) <= asof
    )
    M["pace_per_month_deals"] = pace_deals

    pace_stores = 0
    if M["has_store_data"]:
        for s in real_stores:
            if (s.get("status") or "").lower() != "active":
                continue
            ts = parse_dt(s.get("activated_at"))
            if ts and month_ago <= ts <= asof:
                pace_stores += 1
    M["pace_per_month_stores"] = pace_stores

    M["pace_per_month"] = pace_stores if M["has_store_data"] else pace_deals
    gap = max(0, GOAL - M["active_stores"])
    M["required_pace"]      = math.ceil(gap / months_left) if months_left > 0 else 0
    M["pace_gap_multiple"]  = round(M["required_pace"] / max(1, M["pace_per_month"])) if M["pace_per_month"] else None
    M["projected_total"]    = M["active_stores"] + (M["pace_per_month"] * months_left)
    M["shortfall"]          = max(0, GOAL - M["projected_total"])

    # Stalled deals — bucketed by days in current stage.
    # Buckets reflect distinct operational responses:
    #   30-59  Nudge zone:  ping owner, get blocker logged
    #   60-89  Action zone: owner needs to commit a path, escalate if vendor-blocked
    #   90+    Crisis zone: reassign or kill
    # The headline number on Page 1 is "30+ stalled" (sum of all three buckets).
    terminal = {"closedwon", "closedlost"}
    stalled_30_all = []   # everything 30+ days, all stages
    for d in deals:
        if d.stage in terminal:
            continue
        days = d.days_in_stage(asof)
        if days is not None and days >= 30:
            stalled_30_all.append((d, days))
    stalled_30_all.sort(key=lambda x: -x[1])
    M["stalled_30d_all"] = stalled_30_all

    # Page-1 view: exclude top-of-funnel (Leads, Unqualified, Parking Lot)
    # because deals can legitimately sit there for a while.
    stalled_30_active = [
        (d, days) for d, days in stalled_30_all
        if d.stage not in EARLY_FUNNEL_STAGES
    ]
    M["stalled_30d_active"] = stalled_30_active

    # Bucket the active-funnel stalled deals
    bucket_30_59 = [(d, dd) for d, dd in stalled_30_active if 30 <= dd < 60]
    bucket_60_89 = [(d, dd) for d, dd in stalled_30_active if 60 <= dd < 90]
    bucket_90p   = [(d, dd) for d, dd in stalled_30_active if dd >= 90]

    M["stalled_buckets"] = {
        "30_59": {
            "deals":  bucket_30_59,
            "count":  len(bucket_30_59),
            "stores": sum(d.amount for d, _ in bucket_30_59),
        },
        "60_89": {
            "deals":  bucket_60_89,
            "count":  len(bucket_60_89),
            "stores": sum(d.amount for d, _ in bucket_60_89),
        },
        "90p": {
            "deals":  bucket_90p,
            "count":  len(bucket_90p),
            "stores": sum(d.amount for d, _ in bucket_90p),
        },
    }

    # Aggregate "30+" view — what drives the headline KPI on Page 1.
    M["stalled_30d_stores"]  = sum(d.amount for d, _ in stalled_30_active)
    M["stalled_30d_count"]   = len(stalled_30_active)

    # Backwards-compat aliases (kept so existing references keep working).
    # Treat these as deprecated within the codebase.
    stalled_60_active = bucket_60_89 + bucket_90p
    M["stalled_60d_active"] = stalled_60_active
    M["stalled_60d_stores"] = sum(d.amount for d, _ in stalled_60_active)
    M["stalled_60d_count"]  = len(stalled_60_active)
    M["stalled_60d_all"]    = [
        (d, dd) for d, dd in stalled_30_all if dd >= 60
    ]

    # Two Top-5 tables now: one urgent (60+), one nudge-zone (30-59).
    M["top5_stalled_60p"] = build_top_stalled(stalled_60_active, top=5)
    M["top5_stalled_30_59"] = build_top_stalled(bucket_30_59, top=5)
    # Keep the old name pointing at the urgent table so existing draw code
    # keeps working until refactored.
    M["top5_stalled"] = M["top5_stalled_60p"]

    # For the 'Required next week' column we want the largest stalled groups
    # by store count (where the leverage actually is), plus a summary of the rest.
    all_stalled_groups = group_stalled_deals(stalled_60_active)
    all_stalled_groups.sort(key=lambda r: -r["stores"])
    M["top4_stalled_by_stores"] = all_stalled_groups[:4]
    remaining = all_stalled_groups[4:]
    M["stalled_remaining_groups"] = remaining
    M["stalled_remaining_stores"] = sum(r["stores"] for r in remaining)
    M["stalled_remaining_deals"]  = sum(r["deal_count"] for r in remaining)

    # Forward calendar
    cutoff = asof + timedelta(days=14)
    fwd = [d for d in deals
           if d.next_activity and asof <= d.next_activity <= cutoff
           and d.stage not in terminal]
    fwd.sort(key=lambda d: d.next_activity)
    M["fwd_calendar"]        = fwd
    M["fwd_calendar_count"]  = len(fwd)
    M["fwd_calendar_target"] = 30
    M["fwd_calendar_top5"]   = fwd[:5]

    # Awaiting SW
    sw_deals = sorted(by_stage.get(sw_id, []), key=lambda d: -d.amount)
    M["sw_top5"]              = sw_deals[:5]
    M["sw_total"]             = M["sw_stores"]
    M["sw_uncategorized"]     = sum(d.amount for d in by_stage.get(sw_id, []) if not d.blocked_reason)
    M["sw_uncategorized_pct"] = round(100 * M["sw_uncategorized"] / max(1, M["sw_total"]))

    # Vendor exposure
    vendor_totals    = defaultdict(int)
    vendor_retailers = defaultdict(list)
    sw_categorized   = 0
    for d in by_stage.get(sw_id, []):
        if not d.blocked_reason:
            continue
        sw_categorized += d.amount
        br = d.blocked_reason.lower()
        for vname, kws in VENDOR_PATTERNS.items():
            if any(kw in br for kw in kws):
                vendor_totals[vname] += d.amount
                vendor_retailers[vname].append(
                    (d.name.split(" -")[0].split(" (")[0].strip(), d.amount)
                )
                break
    M["sw_categorized"]   = sw_categorized
    M["vendor_totals"]    = vendor_totals
    M["vendor_retailers"] = vendor_retailers

    # Movement (any deal that entered its current stage in the last 7 days)
    moved = []
    for d in deals:
        if d.entered_current and week_ago <= d.entered_current <= asof:
            moved.append(d)
    moved.sort(key=lambda d: -(d.entered_current.timestamp() if d.entered_current else 0))
    M["moved_this_week"] = moved

    # New deals this week (by createdate)
    new_deals = [d for d in deals if d.created and week_ago <= d.created <= asof]
    M["new_deals_this_week"] = new_deals

    # In-Lab newcomers this week
    in_lab_new = [
        d for d in by_stage.get(in_lab_id, [])
        if d.entered_current and week_ago <= d.entered_current <= asof
    ]
    in_lab_new.sort(key=lambda d: -d.amount)
    M["in_lab_new_this_week"] = in_lab_new

    # Lab median days
    lab_days = []
    for d in by_stage.get(in_lab_id, []):
        days = d.days_in_stage(asof)
        if days is not None:
            lab_days.append(days)
    M["lab_median_days"] = int(sorted(lab_days)[len(lab_days)//2]) if lab_days else 0

    # ============================================================
    # ANOMALIES — Page 3 punch list
    # ============================================================
    A = {}
    cw_deals = by_stage.get("closedwon", [])

    A["test_in_active"]        = [d for d in cw_deals if d.is_test_record()]
    A["test_in_active_stores"] = sum(d.amount for d in A["test_in_active"])

    A["closedwon_no_amount"]   = [d for d in cw_deals if d.amount_missing()]

    A["pipeline_no_amount"] = [
        d for d in deals
        if d.stage not in terminal
        and d.stage not in EARLY_FUNNEL_STAGES
        and d.amount_missing()
    ]
    A["early_funnel_no_amount"] = [
        d for d in deals
        if d.stage in EARLY_FUNNEL_STAGES
        and d.amount_missing()
    ]

    A["zero_amount"] = [
        d for d in deals
        if d.stage not in terminal and d.amount_zero_explicit()
    ]

    A["test_in_pipeline"] = [
        d for d in deals
        if d.stage not in terminal and d.is_test_record()
    ]

    A["no_owner"] = [d for d in deals if d.stage not in terminal and not d.owner]

    A["sw_no_reason"]        = [d for d in by_stage.get(sw_id, []) if not d.blocked_reason]
    A["sw_no_reason_stores"] = sum(d.amount for d in A["sw_no_reason"])

    name_counts = defaultdict(list)
    for d in deals:
        if d.stage not in terminal and d.name:
            name_counts[d.name].append(d)
    A["duplicates"] = [(name, lst) for name, lst in name_counts.items() if len(lst) > 1]

    stale_early = []
    for d, days in stalled_30_all:
        if d.stage in EARLY_FUNNEL_STAGES and days >= 90:
            stale_early.append((d, days))
    stale_early.sort(key=lambda x: -x[1])
    A["stale_early"] = stale_early

    # Anomaly 1 supporting numbers — the gap between deal Amount sums and
    # actual store count.
    #   - active_legit_sum: closed-won deal-Amount sum, excluding test records.
    #     This is what the deal records "say" the active store count is.
    #   - active_stores_real (computed above): what HubSpot Stores actually
    #     reports. The gap is the data-quality issue.
    A["active_legit_sum"] = M["active_stores_deals"] - A["test_in_active_stores"]

    M["anomalies"] = A

    # ============================================================
    # FUNNEL BREAKDOWN — used by Page 1 stage-distribution chart
    # and Page 2 stage funnel table.
    # ============================================================
    M["funnel_breakdown"] = build_funnel_breakdown(M, asof)

    return M


# Funnel-order: top of funnel → bottom. Stages with 0 deals are still
# shown so the report has a consistent skeleton. (We filter empties later.)
# Funnel order — defined by role keys (matches keys in STAGE_DISPLAY_FULL).
# Display labels are pulled from the central registry, so renaming a label
# in HubSpot doesn't require updating this list.
FUNNEL_ORDER_KEYS = [
    "_leads",
    "_unqualified",
    "_qualified",
    "_get_started",
    "_setup_guide",
    "_legals",
    "onboarding_stage_id",
    "in_lab_stage_id",
    "awaiting_sw_stage_id",
    "awaiting_activation_stage_id",
    "awaiting_transactions_stage_id",
    "_active",
    "_closed_lost",
    "_parking_lot_other",
]


def build_funnel_breakdown(M, asof):
    """Produce a list of (label, deals_count, stores, median_days_in_stage)
    rows in funnel order, restricted to stages that have deals.

    Stages are matched to funnel positions via role-IDs (preferred) or
    label-fragment fallback. Display labels come from the central registry.
    """
    import statistics
    by_stage = M["by_stage"]
    payload = M.get("payload") or {}

    rows = []
    used_stage_ids = set()
    for role_key in FUNNEL_ORDER_KEYS:
        # Find the stage_id for this role
        if role_key == "_active":
            stage_ids = list(payload.get("active_stage_ids") or [])
        elif role_key in ("in_lab_stage_id", "awaiting_sw_stage_id",
                          "awaiting_activation_stage_id",
                          "awaiting_transactions_stage_id",
                          "onboarding_stage_id"):
            sid = payload.get(role_key)
            stage_ids = [sid] if sid else []
        else:
            # Fragment-matched roles — find any stage_id that resolves to this key
            stage_ids = [
                sid for sid in (payload.get("stage_labels") or {})
                if _stage_role_key(sid, payload) == role_key
            ]

        for sid in stage_ids:
            if not sid or sid in used_stage_ids:
                continue
            deals_at = by_stage.get(sid, [])
            if not deals_at:
                continue
            used_stage_ids.add(sid)
            n = len(deals_at)
            stores = sum(d.amount for d in deals_at)
            days_list = [d.days_in_stage(asof) for d in deals_at
                         if d.days_in_stage(asof) is not None]
            median_days = int(statistics.median(days_list)) if days_list else None
            rows.append({
                "label": stage_short(sid, payload),  # short for compact tables
                "label_full": stage_display(sid, payload),
                "stage_id": sid,
                "deals": n,
                "stores": stores,
                "median_days": median_days,
            })
    return rows


def group_stalled_deals(stalled_pairs):
    """Group rollouts (R1, R2/6, etc.) of same retailer into one row.
    Returns list of group dicts, unsorted."""
    by_group = defaultdict(list)
    for d, days in stalled_pairs:
        base = re.sub(r"\s*[\(\-]\s*R\d+/?\d*\s*[\)\-]?\s*$", "", d.name).strip()
        base = re.sub(r"\s*-\s*New Deal.*$", "", base).strip()
        base = re.sub(r"\s*-\s*New Date.*$", "", base).strip()
        by_group[base].append((d, days))
    rows = []
    for base, items in by_group.items():
        total_stores = sum(d.amount for d, _ in items)
        days_min = min(days for _, days in items)
        days_max = max(days for _, days in items)
        days_str = f"{days_min}" if days_min == days_max else f"{days_min}–{days_max}"
        stages = set(d.stage for d, _ in items)
        reasons = sorted({d.blocked_reason for d, _ in items if d.blocked_reason})
        reason = "; ".join(reasons) if reasons else "—"
        suffix = f" · all {len(items)} rollouts" if len(items) > 1 else ""
        rows.append({
            "name": f"{base}{suffix}",
            "base_name": base,
            "is_group": len(items) > 1,
            "count": len(items),
            "deal_count": len(items),
            "stage": next(iter(stages)),
            "stores": total_stores,
            "days": days_str,
            "days_max": days_max,
            "reason": reason,
        })
    return rows


def build_top_stalled(stalled_pairs, top=5):
    """Top-N stalled groups sorted by (days DESC, stores DESC tiebreaker)."""
    rows = group_stalled_deals(stalled_pairs)
    rows.sort(key=lambda r: (-r["days_max"], -r["stores"]))
    return rows[:top]


def build_top_stalled_by_stores(stalled_pairs, top=4):
    """Top-N stalled groups sorted by stores DESC. Used for the
    'Required next week to bend the curve' column where the goal is
    largest unblocking opportunity."""
    rows = group_stalled_deals(stalled_pairs)
    rows.sort(key=lambda r: -r["stores"])
    return rows[:top]


# ============================================================
# DRAWING HELPERS
# ============================================================
def draw_header(c, asof_label, pulled_at_str, page_subtitle=None):
    """Title row + navy underline rule (Convenience.org style).
    The title sits inside the page margins; the rule spans MARGIN_L to MARGIN_R.
    """
    title_baseline_y = PAGE_H - 0.45 * inch
    rule_y          = PAGE_H - 0.58 * inch

    # Title — navy, large
    c.setFillColor(NAVY); c.setFont("Helvetica-Bold", 18)
    c.drawString(MARGIN_L, title_baseline_y, "TruAge Activation Report")

    # Right side — week ending + pull source (stacked above rule)
    c.setFont("Helvetica", 9); c.setFillColor(INK_SOFT)
    c.drawRightString(PAGE_W - MARGIN_R, title_baseline_y + 0.06 * inch,
                      f"Week ending {asof_label}")
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    c.drawRightString(PAGE_W - MARGIN_R, title_baseline_y - 0.08 * inch,
                      f"Source: HubSpot live pull, {pulled_at_str}")

    # Navy underline — 2pt, full content width
    c.setStrokeColor(HEADER_RULE); c.setLineWidth(2.0)
    c.line(MARGIN_L, rule_y, PAGE_W - MARGIN_R, rule_y)

    # Optional page subtitle — sits below the rule, right-aligned, navy
    if page_subtitle:
        c.setFont("Helvetica-Bold", 9); c.setFillColor(NAVY)
        c.drawRightString(PAGE_W - MARGIN_R, rule_y - 0.18 * inch,
                          page_subtitle.upper())


def draw_footer(c, page_num, total, page_label, footer_extra=""):
    c.setStrokeColor(RULE_SOFT); c.setLineWidth(0.5)
    c.line(MARGIN_L, MARGIN_B + 0.15*inch, PAGE_W - MARGIN_R, MARGIN_B + 0.15*inch)
    c.setFont("Helvetica", 8); c.setFillColor(INK_FAINT)
    left_text = f"TruAge Activation Report · Page {page_num} of {total} · {page_label}"
    c.drawString(MARGIN_L, MARGIN_B - 0.02*inch, left_text)
    if footer_extra:
        c.drawRightString(PAGE_W - MARGIN_R, MARGIN_B - 0.02*inch, footer_extra)


def section_label(c, x, y, label, color=NAVY):
    """Default section label color is brand navy. Coral (ACCENT_DARK) is
    reserved for alert/required-action contexts and must be passed explicitly."""
    c.setFont("Helvetica-Bold", 9); c.setFillColor(color)
    c.drawString(x, y, "■ " + label.upper())


# ============================================================
# STAGE LABEL REGISTRY — single source of truth
# ============================================================
# HubSpot stage labels can change (e.g. "In Lab" → "Parking Lot 2 Lab",
# "Awaiting SW" → "Parking Lot 1 Awaiting SW"). Rather than mirror those
# noisy labels everywhere, the report uses canonical role-based names
# that capture operational meaning. Every rendering site goes through
# stage_display() / stage_short() so labels stay consistent across the
# document.
#
# DISPLAY (full)  — used in headlines, KPI cards, trajectory band, prose
# SHORT           — used in narrow table columns, chart axis labels
#
# When neither role nor label-fragment matches, we fall back to whatever
# HubSpot returned (so unknown stages are at least visible, not blank).

STAGE_DISPLAY_FULL = {
    # role-key (matches keys in payload like 'in_lab_stage_id')
    "in_lab_stage_id":               "In Lab",
    "awaiting_sw_stage_id":          "Awaiting Software",
    "awaiting_activation_stage_id":  "Awaiting Activation",
    "awaiting_transactions_stage_id":"Awaiting Transactions",
    "onboarding_stage_id":           "Onboarding",
    # Active is a list (active_stage_ids) — handled separately
    "_active":                        "Active",
    # Stages without role assignments — matched by label fragment
    "_qualified":                     "Qualified",
    "_get_started":                   "Get Started Form",
    "_setup_guide":                   "Setup Guide",
    "_legals":                        "Legals Signed",
    "_leads":                         "Leads",
    "_unqualified":                   "Unqualified",
    "_closed_lost":                   "Closed Lost",
    "_parking_lot_other":             "Parking Lot Other",
}

# Short labels — used where horizontal space is tight (table columns, chart x-axis).
# Keys match STAGE_DISPLAY_FULL.
STAGE_DISPLAY_SHORT = {
    "in_lab_stage_id":               "In Lab",
    "awaiting_sw_stage_id":          "Await. SW",
    "awaiting_activation_stage_id":  "Await. Act.",
    "awaiting_transactions_stage_id":"Await. Trans",
    "onboarding_stage_id":           "Onboarding",
    "_active":                        "Active",
    "_qualified":                     "Qualified",
    "_get_started":                   "Get Started Form",
    "_setup_guide":                   "Setup Guide",
    "_legals":                        "Legals Signed",
    "_leads":                         "Leads",
    "_unqualified":                   "Unqualified",
    "_closed_lost":                   "Closed Lost",
    "_parking_lot_other":             "Parking Lot",
}


def _stage_role_key(stage_id, payload):
    """Return the canonical role key for a stage_id, or None if unknown.

    Resolution order:
      1. Role-based (payload role IDs — stable across HubSpot label changes)
      2. Label-fragment match on the live HubSpot label
    """
    if not payload:
        return None
    if stage_id in (payload.get("active_stage_ids") or []):
        return "_active"
    role_keys = ("in_lab_stage_id", "awaiting_sw_stage_id",
                 "awaiting_activation_stage_id",
                 "awaiting_transactions_stage_id", "onboarding_stage_id")
    for k in role_keys:
        if payload.get(k) == stage_id:
            return k
    # Label-fragment fallback — for stages without role assignments
    label = (payload.get("stage_labels") or {}).get(stage_id, "") or ""
    norm = " ".join(label.lower().split())
    if "qualified" in norm and "unqualified" not in norm:
        return "_qualified"
    if "get started" in norm:
        return "_get_started"
    if "setup guide" in norm:
        return "_setup_guide"
    if "legals" in norm:
        return "_legals"
    if "unqualified" in norm:
        return "_unqualified"
    if "leads" in norm:
        return "_leads"
    if "closed lost" in norm:
        return "_closed_lost"
    if "parking" in norm:
        return "_parking_lot_other"
    if "account activated" in norm or norm == "active":
        return "_active"
    return None


def stage_display(stage_id, payload):
    """Full canonical display label for a stage (e.g. 'Awaiting Software')."""
    role = _stage_role_key(stage_id, payload)
    if role and role in STAGE_DISPLAY_FULL:
        return STAGE_DISPLAY_FULL[role]
    # Fallback — return live label, normalized whitespace
    raw = (payload.get("stage_labels") or {}).get(stage_id, stage_id) or stage_id
    return " ".join(str(raw).split())


def stage_short(stage_id, payload):
    """Short canonical display label for narrow contexts."""
    role = _stage_role_key(stage_id, payload)
    if role and role in STAGE_DISPLAY_SHORT:
        return STAGE_DISPLAY_SHORT[role]
    raw = (payload.get("stage_labels") or {}).get(stage_id, stage_id) or stage_id
    return " ".join(str(raw).split())


# Section-box layout constants (used by Page 1 boxed sections)
BOX_TITLE_H        = 0.22 * inch  # height of title strip inside the box
BOX_BORDER_COLOR   = RULE         # 0.5px navy-tinted gray
BOX_HEADER_RULE_COLOR = RULE_SOFT
BOX_PAD_X          = 0.10 * inch
BOX_PAD_TOP        = 0.06 * inch
BOX_PAD_BOTTOM     = 0.08 * inch


def draw_section_box(c, x, y_top, width, height, title, subtitle=None,
                     title_color=NAVY):
    """Draw a bordered section card with a title strip at the top.

    Returns the y-coordinate where inner content should begin drawing (the
    top of the content area, inside the box, below the title rule).
    The caller is responsible for fitting content within the height — this
    function just paints the frame.
    """
    # Outer border
    c.setFillColor(white)
    c.setStrokeColor(BOX_BORDER_COLOR); c.setLineWidth(0.5)
    c.rect(x, y_top - height, width, height, fill=1, stroke=1)

    # Title text — sits inside the box, top-left
    c.setFont("Helvetica-Bold", 9); c.setFillColor(title_color)
    title_y = y_top - 0.16 * inch
    c.drawString(x + BOX_PAD_X, title_y, "■ " + title.upper())

    # Optional subtitle on the right side of the title row
    if subtitle:
        c.setFont("Helvetica-Oblique", 7); c.setFillColor(INK_FAINT)
        c.drawRightString(x + width - BOX_PAD_X, title_y, subtitle)

    # Thin rule under the title row
    rule_y = y_top - BOX_TITLE_H
    c.setStrokeColor(BOX_HEADER_RULE_COLOR); c.setLineWidth(0.5)
    c.line(x + BOX_PAD_X, rule_y, x + width - BOX_PAD_X, rule_y)

    # Return content-start y
    return rule_y - BOX_PAD_TOP

def short_name(name, max_len=42):
    """Truncate a deal name to fit a column, on a word boundary where possible.

    Trim trailing " - <suffix>" and " (<suffix>)" decorations first (they're
    almost always rollout indicators or duplicate-name disambiguators).
    Then if still too long, cut to the last full word that fits.
    """
    n = name.split(" - ")[0]
    n = n.split(" (")[0]
    if len(n) <= max_len:
        return n
    # Truncate on word boundary — find the last space within budget
    cut = n[:max_len].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"

def short_stage(stage_id, stage_labels, payload=None):
    """Legacy interface — delegates to stage_short() against the registry.

    Kept so existing call sites work unchanged. New code should call
    stage_short(stage_id, payload) directly.
    """
    if payload is None:
        # Reconstruct minimal payload from stage_labels for fallback
        payload = {"stage_labels": stage_labels or {}}
    return stage_short(stage_id, payload)


# ============================================================
# CHART RENDERS
# ============================================================
def render_pipeline_bar(M, path):
    """Layered horizontal bar — Active | In Progress | Await SW | gap to 25K.
    Labels stack above and below the bar to avoid collisions in narrow segments."""
    fig, ax = plt.subplots(figsize=(7.6, 1.5), dpi=160)
    fig.patch.set_facecolor('white')

    in_prog_pre_sw  = M["in_lab_stores"] + M["onb_stores"]
    sw              = M["sw_stores"]
    in_prog_post_sw = M["act_stores"] + M["trans_stores"]
    active          = M["active_stores"]
    used = active + in_prog_pre_sw + sw + in_prog_post_sw
    gap = max(0, GOAL - used)

    segments = [
        ("Active",      active,            "#0F6E56"),
        ("In Progress", in_prog_pre_sw,    "#7eb074"),
        ("Await. SW",   sw,                "#e8a05a"),
    ]
    if in_prog_post_sw > 0:
        segments.append(("Await. Act/Trans", in_prog_post_sw, "#c9a23a"))
    segments.append(("Pipeline gap", gap, "#dde3ec"))

    bar_y = 0.0
    bar_height = 0.40
    left = 0
    for name, val, color in segments:
        ax.barh(bar_y, val, left=left, color=color, edgecolor='white',
                linewidth=0.7, height=bar_height)
        left += val

    # Stack labels: alternate above (positive y) and below (negative y).
    # The "Active" callout already lives above-left; offset other above-labels.
    above_y = 0.42
    below_y = -0.42
    label_above_y = 0.62  # for label text
    label_below_y = -0.62

    left = 0
    for i, (name, val, color) in enumerate(segments):
        if val <= 0:
            left += val
            continue
        center = left + val / 2
        is_gap = "gap" in name
        is_active = name == "Active"
        label_color = "#555" if is_gap else color
        label_text = f"{name} {val:,}"

        # Active gets its own treatment (callout above, no inline)
        if is_active:
            left += val
            continue

        # Alternate: i=1 (In Progress) above, i=2 (Await SW) below,
        # i=3 (Await Act/Trans) above, i=4 (Pipeline gap) above.
        # This keeps "Active" tag (which is below) on its own row.
        place_above = (i % 2 == 1)
        if is_gap:
            place_above = True

        y_label = label_above_y if place_above else label_below_y
        y_tick_start = bar_y + bar_height/2 if place_above else bar_y - bar_height/2
        y_tick_end = y_label - 0.05 if place_above else y_label + 0.05
        va = 'bottom' if place_above else 'top'

        # Tick line from bar to label
        ax.plot([center, center], [y_tick_start, y_tick_end],
                color="#aaaaaa", linewidth=0.5)
        ax.text(center, y_label, label_text,
                ha='center', va=va, color=label_color,
                fontsize=7.5, fontweight='bold')
        left += val

    # Active callout above-left — sits on its own row, clearly above all
    # inline labels so it doesn't collide with "In Progress" or others.
    # Stacking: big number on top, "+N this week" delta directly below in green.
    delta = M["active_delta_week"]
    callout_y_main = label_above_y + 0.95   # big number row
    callout_y_sub  = label_above_y + 0.55   # delta row beneath it
    ax.text(0, callout_y_main, f"{active:,}", ha='left', va='center',
            color="#0F6E56", fontsize=13, fontweight='bold')
    if delta:
        ax.text(0, callout_y_sub, f"+{delta:,} this week",
                ha='left', va='center',
                color="#0F6E56", fontsize=8)
    # Small "Active" leader pointing to the green segment.
    if active > 0:
        center_active = active / 2
        ax.plot([center_active, center_active], [bar_y - bar_height/2, label_below_y + 0.05],
                color="#aaaaaa", linewidth=0.5)
        ax.text(center_active, label_below_y, "Active",
                ha='center', va='top', color="#0F6E56",
                fontsize=7.5, fontweight='bold')

    # Goal marker
    ax.axvline(GOAL, color="#1a1a1a", linewidth=0.8, linestyle=(0, (3, 2)))
    ax.text(GOAL, label_above_y + 0.30, f"{GOAL//1000}K",
            ha='right', va='center', color="#1a1a1a",
            fontsize=9, fontweight='bold')

    ax.set_xlim(0, GOAL)
    ax.set_ylim(-1.0, 2.0)   # widened upper bound for the stacked callout
    ax.set_yticks([])
    ax.set_xticks([0, 5000, 10000, 15000, 20000, 25000])
    ax.set_xticklabels(['0', '5K', '10K', '15K', '20K', '25K'], fontsize=7, color='#555')
    for sp in ('top', 'right', 'left'): ax.spines[sp].set_visible(False)
    ax.spines['bottom'].set_color('#cccccc')
    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches='tight', facecolor='white', pad_inches=0.05)
    plt.close()


def render_trend_chart(M, path):
    fig, ax = plt.subplots(figsize=(7.6, 2.2), dpi=160)
    fig.patch.set_facecolor('white')
    asof = M["asof"]; goal = GOAL

    cw = sorted(
        (d for d in M["by_stage"].get("closedwon", [])
         if (d.stage_entries.get("closedwon") or d.closed)),
        key=lambda d: d.stage_entries.get("closedwon") or d.closed,
    )
    cum = 0; pts = []
    for d in cw:
        ts = d.stage_entries.get("closedwon") or d.closed
        cum += d.amount
        pts.append((ts, cum))
    if pts:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    else:
        xs = [asof]; ys = [M["active_stores"]]

    pace = M["pace_per_month"]; months_left = M["months_left"]
    proj_xs = [asof + timedelta(days=30*i) for i in range(months_left + 1)]
    proj_ys = [M["active_stores"] + i * pace for i in range(months_left + 1)]
    req_xs = [asof, GOAL_DATE]; req_ys = [M["active_stores"], goal]

    ax.plot(xs, ys, color="#0F6E56", linewidth=2.0, label="Actuals")
    ax.plot(proj_xs, proj_ys, color="#C04828", linewidth=1.8, linestyle='--',
            label=f"Current pace → {M['projected_total']:,}")
    ax.plot(req_xs, req_ys, color="#1a1a1a", linewidth=1.4, linestyle=':',
            label=f"Required {M['required_pace']:,}/mo")

    ax.plot(asof, M["active_stores"], 'o', color="#0F6E56", markersize=6)
    ax.annotate(f"TODAY  {M['active_stores']:,}",
                xy=(asof, M["active_stores"]),
                xytext=(asof - timedelta(days=120), M["active_stores"] + 1700),
                fontsize=7.5, color="#1a1a1a", ha='center',
                arrowprops=dict(arrowstyle='-', color='#999', lw=0.5))

    ax.annotate(f"→ {M['projected_total']:,}",
                xy=(GOAL_DATE, M['projected_total']),
                xytext=(8, 0), textcoords='offset points',
                fontsize=7.5, color="#C04828", va='center')
    ax.annotate(f"→ {goal:,}",
                xy=(GOAL_DATE, goal),
                xytext=(8, 0), textcoords='offset points',
                fontsize=7.5, color="#1a1a1a", fontweight='bold', va='center')

    mid_x = asof + timedelta(days=int((GOAL_DATE - asof).days * 0.55))
    mid_y = M["active_stores"] + (goal - M["active_stores"]) * 0.55
    ax.annotate(f"Required: {M['required_pace']:,}/mo\nto reach {goal:,} by {GOAL_DATE_STR}",
                xy=(mid_x, mid_y),
                fontsize=7.5, color="#8B2418", ha='center')

    ax.set_ylim(0, max(28000, goal + 3000))
    ax.set_yticks([1000, 5000, 10000, 25000])
    ax.set_yticklabels(['1K', '5K', '10K', '25K'], fontsize=7, color='#555')
    import matplotlib.dates as mdates
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b'%y"))
    ax.tick_params(axis='x', labelsize=7, colors='#555')
    for sp in ('top','right'): ax.spines[sp].set_visible(False)
    for sp in ('bottom','left'): ax.spines[sp].set_color('#cccccc')
    ax.grid(axis='y', linestyle=':', linewidth=0.4, color='#dddddd')
    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches='tight', facecolor='white', pad_inches=0.05)
    plt.close()


def render_vendor_bar(M, path):
    """Single horizontal stacked bar of vendor exposure for SW dependencies.
    Replaces the pie — same information, far more space-efficient.
    Each segment labeled inline with vendor name + store count + percent."""
    fig, ax = plt.subplots(figsize=(7.6, 0.95), dpi=160)
    fig.patch.set_facecolor('white')
    sorted_vendors = sorted(M["vendor_totals"].items(), key=lambda kv: -kv[1])
    if not sorted_vendors:
        ax.text(0.5, 0.5, "No categorized SW data", ha='center', va='center',
                fontsize=8, color='#888', transform=ax.transAxes)
        ax.axis('off')
        plt.tight_layout(); plt.savefig(path, dpi=160, bbox_inches='tight',
                                        facecolor='white'); plt.close()
        return

    values = [v[1] for v in sorted_vendors]
    labels = [v[0] for v in sorted_vendors]
    total = sum(values)

    # Distinct segment colors from the Convenience family — navy, teal, coral, gold.
    # These read clearly against each other.
    vendor_palette = ["#1F3864", "#0F6E56", "#C04828", "#C9A23A"]
    colors = [vendor_palette[i % len(vendor_palette)] for i in range(len(values))]

    bar_y = 0.5
    bar_h = 0.65
    left = 0
    for val, color in zip(values, colors):
        ax.barh(bar_y, val, left=left, color=color, edgecolor='white',
                linewidth=1.2, height=bar_h)
        left += val

    # Inline labels — large enough to actually be legible
    left = 0
    min_inline_pct = 0.18  # segment must be ≥18% of total to fit name inline
    for val, color, label in zip(values, colors, labels):
        seg_pct = val / max(1, total)
        center = left + val / 2
        pct_int = round(100 * seg_pct)
        short_label = label.split(" (")[0]
        if seg_pct >= min_inline_pct:
            text = f"{short_label}  {val:,}  ({pct_int}%)"
            ax.text(center, bar_y, text,
                    ha='center', va='center', color='white',
                    fontsize=10, fontweight='bold')
        else:
            ax.text(center, bar_y, f"{pct_int}%",
                    ha='center', va='center', color='white',
                    fontsize=9, fontweight='bold')
            ax.text(center, bar_y - 0.55, f"{short_label} {val:,}",
                    ha='center', va='top', color=color,
                    fontsize=8.5, fontweight='bold')
        left += val

    ax.set_xlim(0, total)
    ax.set_ylim(-0.6, 1.1)
    ax.set_yticks([])
    ax.set_xticks([])
    for sp in ('top', 'right', 'left', 'bottom'):
        ax.spines[sp].set_visible(False)
    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches='tight', facecolor='white',
                pad_inches=0.05)
    plt.close()


def render_stage_distribution(M, path):
    """Vertical-bar chart of deal counts per funnel stage, in funnel order.
    Used on Page 1 to show 'where do the deals sit?' at a glance."""
    rows = M["funnel_breakdown"]
    if not rows:
        # Empty fallback
        fig, ax = plt.subplots(figsize=(7.6, 1.2), dpi=160)
        ax.text(0.5, 0.5, "No funnel data", ha='center', va='center',
                fontsize=8, color='#888', transform=ax.transAxes)
        ax.axis('off')
        plt.tight_layout(); plt.savefig(path, dpi=160, bbox_inches='tight',
                                        facecolor='white'); plt.close()
        return

    fig, ax = plt.subplots(figsize=(7.6, 1.4), dpi=160)
    fig.patch.set_facecolor('white')

    labels = [r["label"] for r in rows]
    counts = [r["deals"] for r in rows]
    n_bars = len(rows)

    # Color logic: funnel-position gradient — top of funnel pale gray,
    # post-qualification deeper blue, active green. We just have to assign
    # by row index since rows already come in funnel order.
    colors = []
    # Find indices of the key stages for color anchoring
    for r in rows:
        lbl = r["label"]
        if lbl == "Active":
            colors.append("#0F6E56")
        elif lbl in ("Closed Lost",):
            colors.append("#bbbbbb")
        elif lbl in ("Parking Lot",):
            colors.append("#aaaaaa")
        elif lbl in ("Await. SW", "Await. Act.", "Await. Trans"):
            colors.append("#e8a05a")
        elif lbl in ("In Lab", "Onboarding"):
            colors.append("#7eb074")
        else:  # top of funnel: leads, qualified, get started, setup guide
            colors.append("#a8b8c8")

    xs = list(range(n_bars))
    bars = ax.bar(xs, counts, color=colors, edgecolor='white', linewidth=0.5,
                  width=0.78)

    # Value label on top of each bar
    max_h = max(counts) if counts else 1
    for x, count, color in zip(xs, counts, colors):
        ax.text(x, count + max_h * 0.04, f"{count}",
                ha='center', va='bottom',
                fontsize=8, fontweight='bold', color=color)

    # X-axis labels — keep horizontal, small, two-line for long labels
    ax.set_xticks(xs)
    wrapped = []
    for lbl in labels:
        # Wrap labels with > 1 word into two lines if total length > 9 chars
        if " " in lbl and len(lbl) > 9:
            parts = lbl.split(" ", 1)
            wrapped.append(parts[0] + "\n" + parts[1])
        else:
            wrapped.append(lbl)
    ax.set_xticklabels(wrapped, fontsize=6.5, color="#444")
    ax.tick_params(axis='x', length=0, pad=2)

    # Hide y-axis — values are on top of bars
    ax.set_yticks([])
    ax.set_ylim(0, max_h * 1.20)
    ax.set_xlim(-0.6, n_bars - 0.4)
    for sp in ('top', 'right', 'left'):
        ax.spines[sp].set_visible(False)
    ax.spines['bottom'].set_color('#cccccc')

    plt.tight_layout()
    plt.savefig(path, dpi=160, bbox_inches='tight', facecolor='white',
                pad_inches=0.05)
    plt.close()


# ============================================================
# PAGE 1 — STATUS
# ============================================================
def page1(c, M, payload):
    stage_labels = payload["stage_labels"]
    asof_label = M["asof_label"]
    pulled_at = fmt_date((parse_dt(M["pulled_at"]) or M["asof"]), "%b %-d, %Y")
    draw_header(c, asof_label, pulled_at)

    y = PAGE_H - 0.95*inch

    # ---- TOP BAND: Trajectory Gap | Stalled Commitments ----
    band_h = 1.05 * inch
    col_gap = 0.15 * inch
    col_w = (CONTENT_W - col_gap) / 2

    # Trajectory band — cream fill, navy text (the brand identity color)
    c.setFillColor(BG_BAND); c.setStrokeColor(BG_BAND); c.setLineWidth(0)
    c.rect(MARGIN_L, y - band_h, col_w, band_h, fill=1, stroke=0)

    # Stalled band — pink fill, coral text (alert/needs-attention)
    c.setFillColor(BG_STALLED); c.setStrokeColor(BG_STALLED); c.setLineWidth(0)
    c.rect(MARGIN_L + col_w + col_gap, y - band_h, col_w, band_h, fill=1, stroke=0)

    # Section labels: navy for trajectory, coral for stalled
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(NAVY)
    c.drawString(MARGIN_L + 0.12*inch, y - 0.18*inch, "■ 2026 TRAJECTORY GAP")
    c.setFillColor(ACCENT_DARK)
    c.drawString(MARGIN_L + col_w + col_gap + 0.12*inch, y - 0.18*inch, "■ STALLED COMMITMENTS")

    # Big numbers: navy on both bands (consistent emphasis on the metric itself)
    c.setFont("Helvetica-Bold", 14); c.setFillColor(NAVY)
    c.drawString(MARGIN_L + 0.12*inch, y - 0.42*inch,
                 f"Tracking to {M['projected_total']:,} · Goal {GOAL:,}")
    c.drawString(MARGIN_L + col_w + col_gap + 0.12*inch, y - 0.42*inch,
                 f"{M['stalled_30d_stores']:,} stores stuck 30+ days")

    # Stalled bucket breakdown — small, sits beneath the big number
    b = M["stalled_buckets"]
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    bucket_x = MARGIN_L + col_w + col_gap + 0.12*inch
    bucket_y = y - 0.60*inch
    c.drawString(bucket_x, bucket_y,
                 f"30–59d: ")
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(INK)
    c.drawString(bucket_x + 0.46*inch, bucket_y, f"{b['30_59']['stores']:,}")
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    c.drawString(bucket_x + 0.85*inch, bucket_y,
                 "  ·  60–89d: ")
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(INK)
    c.drawString(bucket_x + 1.55*inch, bucket_y, f"{b['60_89']['stores']:,}")
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    c.drawString(bucket_x + 1.95*inch, bucket_y,
                 "  ·  90+d: ")
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(ACCENT_DARK)
    c.drawString(bucket_x + 2.50*inch, bucket_y, f"{b['90p']['stores']:,}")

    # Below the buckets: the longest-stuck deal name (high-leverage focus)
    longest_y = bucket_y - 0.16*inch

    # Detail line: "{n} mo left.  Pace: <bold>X/mo</bold>  Required: <bold>Y/mo</bold> (Z×)"
    x = MARGIN_L + 0.12*inch
    yy = y - 0.62*inch
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    prefix = f"{M['months_left']} mo left.  Pace: "
    c.drawString(x, yy, prefix)
    x_after = x + c.stringWidth(prefix, "Helvetica", 8.5)
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(INK)
    pace_str = f"{M['pace_per_month']}/mo"
    c.drawString(x_after, yy, pace_str)
    x_after += c.stringWidth(pace_str, "Helvetica-Bold", 8.5)
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    c.drawString(x_after, yy, "  Required: ")
    x_after += c.stringWidth("  Required: ", "Helvetica", 8.5)
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(INK)
    req_str = f"{M['required_pace']:,}/mo"
    c.drawString(x_after, yy, req_str)
    x_after += c.stringWidth(req_str, "Helvetica-Bold", 8.5)
    if M["pace_gap_multiple"]:
        gap_str = f" ({M['pace_gap_multiple']}×)"
        c.drawString(x_after, yy, gap_str)

    # Shortfall line
    yy2 = yy - 0.16*inch
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    c.drawString(x, yy2, "Shortfall: ")
    sf_x = x + c.stringWidth("Shortfall: ", "Helvetica", 8.5)
    c.setFont("Helvetica-Bold", 8.5); c.setFillColor(INK)
    c.drawString(sf_x, yy2, f"{M['shortfall']:,} stores")

    # Right side: top stalled descriptor — sits below the bucket breakdown
    rx = MARGIN_L + col_w + col_gap + 0.12*inch
    descriptor_lines = build_stalled_descriptor_lines(M, stage_labels, col_w - 0.24*inch)
    ry = y - 0.78*inch
    for line, is_bold in descriptor_lines[:2]:
        c.setFont("Helvetica-Bold" if is_bold else "Helvetica", 8.5)
        c.setFillColor(INK if is_bold else INK_SOFT)
        c.drawString(rx, ry, line)
        ry -= 0.16*inch

    y -= band_h + 0.18*inch

    # ---- KPI ROW (5 cells) ----
    kpis = build_kpi_row(M)
    draw_kpi_row(c, y, kpis)
    y -= 0.95*inch + 0.18*inch

    # ---- WHAT MOVED / REQUIRED NEXT WEEK ----
    section_label(c, MARGIN_L, y, "What moved this week", color=NAVY)
    section_label(c, MARGIN_L + col_w + col_gap, y, "Required next week to bend the curve", color=ACCENT_DARK)
    y -= 0.16*inch

    moved_lines = format_moved_this_week(M, stage_labels)
    required_lines = format_required_next_week(M)

    style_body = ParagraphStyle('body', fontName='Helvetica', fontSize=8.5,
                                textColor=INK, leading=10.5)

    yy_left = yy_right = y
    for marker, text, color in moved_lines:
        c.setFont("Helvetica-Bold", 9); c.setFillColor(color)
        c.drawString(MARGIN_L, yy_left, marker)
        para = Paragraph(text, style_body)
        avail_w = col_w - 0.18*inch
        _, h_used = para.wrap(avail_w, 1*inch)
        para.drawOn(c, MARGIN_L + 0.18*inch, yy_left - h_used + 0.10*inch)
        yy_left -= max(0.16*inch, h_used + 0.06*inch)

    for marker, text, color in required_lines:
        c.setFont("Helvetica-Bold", 9); c.setFillColor(color)
        c.drawString(MARGIN_L + col_w + col_gap, yy_right, marker)
        para = Paragraph(text, style_body)
        avail_w = col_w - 0.18*inch
        _, h_used = para.wrap(avail_w, 1*inch)
        para.drawOn(c, MARGIN_L + col_w + col_gap + 0.18*inch, yy_right - h_used + 0.10*inch)
        yy_right -= max(0.16*inch, h_used + 0.06*inch)

    y = min(yy_left, yy_right) - 0.10*inch

    # ---- FORWARD CALENDAR | AWAITING SW TOP 5 (boxed) ----
    # Estimate the box height first by measuring the table heights.
    fwd_rows = [["DATE", "DEAL", "STORES"]]
    for d in M["fwd_calendar_top5"]:
        fwd_rows.append([
            fmt_date(d.next_activity, "%b %-d"),
            short_name(d.name, 26),
            f"{d.amount:,}" if d.amount else "—",
        ])
    if len(fwd_rows) == 1:
        fwd_rows.append(["—", "(no scheduled activities)", "—"])
    fwd_tbl = Table(fwd_rows, colWidths=[0.55*inch, col_w - 1.05*inch - 2*BOX_PAD_X, 0.5*inch])
    fwd_tbl.setStyle(table_style())
    fwd_tbl.wrapOn(c, col_w - 2*BOX_PAD_X, 2*inch)
    th_fwd = fwd_tbl._height

    sw_rows = [["RETAILER", "BLOCKER", "STORES"]]
    for d in M["sw_top5"]:
        blocker = d.blocked_reason or "Uncat."
        for vname, kws in VENDOR_PATTERNS.items():
            if any(kw in blocker.lower() for kw in kws):
                blocker = vname.split(" (")[0]; break
        sw_rows.append([short_name(d.name, 22), blocker, f"{d.amount:,}"])
    sw_tbl = Table(sw_rows, colWidths=[col_w - 1.4*inch - 2*BOX_PAD_X, 0.85*inch, 0.5*inch])
    sw_tbl.setStyle(table_style())
    sw_tbl.wrapOn(c, col_w - 2*BOX_PAD_X, 2*inch)
    th_sw = sw_tbl._height

    # Box height — title strip + content + bottom padding, taller of the two
    table_h = max(th_fwd, th_sw)
    box_h = BOX_TITLE_H + BOX_PAD_TOP + table_h + BOX_PAD_BOTTOM

    # Draw the boxes
    inner_y_l = draw_section_box(c, MARGIN_L, y, col_w, box_h,
                                  "Forward Calendar — Next 5")
    inner_y_r = draw_section_box(c, MARGIN_L + col_w + col_gap, y, col_w, box_h,
                                  "Awaiting Software — Top 5 by Retailer")

    # Place tables inside their boxes
    fwd_tbl.drawOn(c, MARGIN_L + BOX_PAD_X, inner_y_l - th_fwd)
    sw_tbl.drawOn(c, MARGIN_L + col_w + col_gap + BOX_PAD_X, inner_y_r - th_sw)

    y -= box_h + 0.20*inch

    # ---- TOP 5 STALLED DEALS — two side-by-side boxes ----
    # LEFT (urgent, 60+ days, navy header)
    # RIGHT (nudge zone, 30-59 days, navy header)
    def build_stalled_rows(top_list, empty_msg):
        rows = [["DEAL", "STAGE", "STORES", "DAYS"]]
        for r in top_list:
            # Grouped rows already announce themselves via name suffix
            # (e.g., "GPM Investments · all 6 rollouts") and via a days
            # range in the DAYS column (e.g., "72-111"). No prefix glyph
            # needed — and avoids font-fallback issues with non-ASCII glyphs.
            nm = short_name(r["name"], 28)
            rows.append([
                nm,
                short_stage(r["stage"], stage_labels, payload),
                f"{r['stores']:,}",
                str(r["days"]),
            ])
        if len(rows) == 1:
            rows.append([empty_msg, "—", "—", "—"])
        return rows

    urgent_rows = build_stalled_rows(M["top5_stalled_60p"], "(none stalled 60+ days)")
    nudge_rows  = build_stalled_rows(M["top5_stalled_30_59"], "(none in 30-59 day zone)")

    inner_w = col_w - 2*BOX_PAD_X
    # Column widths — name and stage need most room. Numeric columns are
    # short content ("1,500", "115") but headers ("STORES", "DAYS") need
    # ~0.55" each to avoid running into each other.
    inner_widths = [inner_w * 0.48, inner_w * 0.26, inner_w * 0.13, inner_w * 0.13]

    urgent_tbl = Table(urgent_rows, colWidths=inner_widths)
    urgent_tbl.setStyle(table_style())
    urgent_tbl.wrapOn(c, inner_w, 2*inch)
    th_urgent = urgent_tbl._height

    nudge_tbl = Table(nudge_rows, colWidths=inner_widths)
    nudge_tbl.setStyle(table_style())
    nudge_tbl.wrapOn(c, inner_w, 2*inch)
    th_nudge = nudge_tbl._height

    table_h = max(th_urgent, th_nudge)
    box_h = BOX_TITLE_H + BOX_PAD_TOP + table_h + BOX_PAD_BOTTOM

    # Subtitles tell the operational story for each bucket
    b = M["stalled_buckets"]
    urgent_total = b["60_89"]["count"] + b["90p"]["count"]
    urgent_stores = b["60_89"]["stores"] + b["90p"]["stores"]
    nudge_count = b["30_59"]["count"]
    nudge_stores = b["30_59"]["stores"]

    inner_y_l = draw_section_box(
        c, MARGIN_L, y, col_w, box_h,
        "Stalled — Urgent (60+ days)",
        subtitle=f"{urgent_total} deals · {urgent_stores:,} stores"
    )
    inner_y_r = draw_section_box(
        c, MARGIN_L + col_w + col_gap, y, col_w, box_h,
        "Stalled — Nudge Zone (30–59d)",
        subtitle=f"{nudge_count} deals · {nudge_stores:,} stores"
    )

    urgent_tbl.drawOn(c, MARGIN_L + BOX_PAD_X, inner_y_l - th_urgent)
    nudge_tbl.drawOn(c, MARGIN_L + col_w + col_gap + BOX_PAD_X,
                     inner_y_r - th_nudge)

    y -= box_h + 0.20*inch

    # ---- PIPELINE DISTRIBUTION CHART (boxed) ----
    chart_h = 1.30*inch
    box_h = BOX_TITLE_H + BOX_PAD_TOP + chart_h + BOX_PAD_BOTTOM
    inner_y = draw_section_box(
        c, MARGIN_L, y, CONTENT_W, box_h,
        "Pipeline Distribution — Deal Count by Stage",
        subtitle=f"{sum(r['deals'] for r in M['funnel_breakdown'])} open and recent deals total"
    )
    chart_path = "/tmp/stage_dist.png"
    render_stage_distribution(M, chart_path)
    c.drawImage(chart_path, MARGIN_L + BOX_PAD_X, inner_y - chart_h,
                width=CONTENT_W - 2*BOX_PAD_X, height=chart_h,
                preserveAspectRatio=True, mask='auto')

    draw_footer(c, 1, 3, "Status",
                "Trend charts, predictive signals & vendor exposure on reverse")


def build_stalled_descriptor_lines(M, stage_labels, max_w):
    if not M["top5_stalled"]:
        return [("No deals stalled 60+ days.", False)]
    top = M["top5_stalled"][0]
    name = short_name(top["name"], 50)
    stores_str = f"{top['stores']:,} store" + ("s" if top["stores"] != 1 else "")
    line1 = f"{name} ({stores_str}) — longest stuck: {top['days']}d"
    parts = []
    # Full canonical label (e.g. "Awaiting Software")
    payload = M.get("payload")
    parts.append(stage_display(top["stage"], payload))
    if top["reason"] != "—":
        parts.append(top["reason"])
    line2 = " · ".join(parts)
    if len(line2) > 80:
        line2 = line2[:77] + "…"
    return [(line1, True), (line2, False)]


def build_kpi_row(M):
    """Build the 5-cell KPI row.

    When store-level data is available (preferred), the row reads:
        Active Stores | Ready | Pending | Awaiting SW | Fwd Cal (14d)
    where Active/Ready/Pending come from the Stores custom object's status field.

    Without store data (older pulls), the row falls back to the deal-Amount
    derived view: Active | In Progress | Awaiting SW | Awaiting Activation | Fwd Cal.

    A '*' is appended to any KPI affected by data anomalies (test records,
    Amount missing, etc.). Page 3 explains each starred KPI in detail.
    """
    A = M["anomalies"]
    by_stage = M["by_stage"]

    def stage_has_test(stage_id):
        return any(d.is_test_record() for d in by_stage.get(stage_id, []))

    # ---- Common subtitles ----
    fwd_sub = f"target {M['fwd_calendar_target']}+"

    sw_sub = ""
    if M["sw_top5"]:
        bits = []
        for d in M["sw_top5"][:2]:
            # Tight char-limit so two retailers + amounts fit on a single line
            bits.append(f"{short_name(d.name, 9)} {d.amount:,}")
        sw_sub = " · ".join(bits)
    sw_star = "*" if stage_has_test(M["sw_id"]) else ""

    # ============================================================
    # STORE-DATA path (preferred when available)
    # ============================================================
    if M["has_store_data"]:
        # Active subtitle: "+N this week" from activated_at
        delta = M["active_delta_week_stores"]
        if delta > 0:
            active_sub = f"+{delta:,} this week"
        else:
            active_sub = "no activations this week"

        # Active gets a * iff there are test records still mixed into deals
        # OR the deal-amount-vs-store-count gap is significant. Page 3
        # surfaces both as anomalies, so the asterisk just nudges the reader.
        active_star = "*" if (A["test_in_active"] or A["closedwon_no_amount"]) else ""

        ready_sub   = "onboarded · not transacting"
        pending_sub = "contracts complete · not onboarded"

        return [
            (f"{M['active_stores_real']:,}{active_star}", "Active Stores",   active_sub),
            (f"{M['ready_stores_real']:,}",                "Ready",            ready_sub),
            (f"{M['pending_stores_real']:,}",              "Pending",          pending_sub),
            (f"{M['sw_stores']:,}{sw_star}",               "Awaiting Software", sw_sub),
            (f"{M['fwd_calendar_count']}",                 "Fwd Calendar (14d)", fwd_sub),
        ]

    # ============================================================
    # DEAL-DATA fallback (when stores not in pull)
    # ============================================================
    active_star = "*" if (A["test_in_active"] or A["closedwon_no_amount"]) else ""
    in_prog_star = "*" if any(stage_has_test(s) for s in
                              (M["in_lab_id"], M["sw_id"], M["act_id"],
                               M["trans_id"], M["onb_id"])) else ""
    act_star = "*" if stage_has_test(M["act_id"]) else ""

    n = M["active_delta_deal_count"]
    delta = M["active_delta_week_deals"]
    if n == 0:
        active_sub = "no closes this week"
    elif delta == 0:
        active_sub = f"{n} closed (amounts not set)"
    else:
        active_sub = f"+{delta:,} this week"

    in_prog_sub = ""
    if M["in_lab_new_this_week"]:
        d = M["in_lab_new_this_week"][0]
        in_prog_sub = f"+{short_name(d.name, 18)} new"

    act_sub = ""
    act_deals = sorted(by_stage.get(M["act_id"], []), key=lambda d: -d.amount)
    if act_deals:
        d = act_deals[0]
        act_sub = f"{short_name(d.name, 16)} {d.amount}"

    return [
        (f"{M['active_stores_deals']:,}{active_star}", "Active Stores",         active_sub),
        (f"{M['in_progress_stores']:,}{in_prog_star}", "Activation in Progress", in_prog_sub),
        (f"{M['sw_stores']:,}{sw_star}",               "Awaiting Software",   sw_sub),
        (f"{M['act_stores']:,}{act_star}",             "Awaiting Activation",    act_sub),
        (f"{M['fwd_calendar_count']}",                 "Fwd Calendar (14d)",     fwd_sub),
    ]


def draw_kpi_row(c, y, kpis):
    n = len(kpis); gap = 0.07 * inch
    box_w = (CONTENT_W - gap*(n-1)) / n
    box_h = 1.02 * inch   # slightly taller — gives 2-line subtitles room to breathe
    for i, (number, label_top, label_bot) in enumerate(kpis):
        x = MARGIN_L + i*(box_w + gap)
        c.setFillColor(BG_KPI); c.setStrokeColor(RULE_SOFT); c.setLineWidth(0.5)
        c.rect(x, y - box_h, box_w, box_h, fill=1, stroke=1)
        # Label on top — small, near-faint
        c.setFont("Helvetica", 7); c.setFillColor(INK_FAINT)
        label_lines = simpleSplit(label_top.upper(), "Helvetica", 7, box_w - 0.18*inch)
        ly = y - 0.16*inch
        for ll in label_lines[:2]:
            c.drawString(x + 0.1*inch, ly, ll); ly -= 0.11*inch
        # Big number — green for Active (positive outcome metric), navy for the rest
        is_active_kpi = label_top.lower().startswith("active")
        number_color = GOOD if is_active_kpi else NAVY
        c.setFont("Helvetica-Bold", 22); c.setFillColor(number_color)
        c.drawString(x + 0.1*inch, y - 0.58*inch, number)
        # Subtitle — pushed lower for clearance from the big number
        c.setFont("Helvetica", 7); c.setFillColor(INK_SOFT)
        sub_lines = simpleSplit(label_bot, "Helvetica", 7, box_w - 0.18*inch)
        sy = y - 0.78*inch
        for sl in sub_lines[:2]:
            c.drawString(x + 0.1*inch, sy, sl); sy -= 0.11*inch


def format_moved_this_week(M, stage_labels):
    lines = []

    for d, ts in M["moved_this_week_active"][:3]:
        name = short_name(d.name, 36)
        date_str = fmt_date(ts, '%b %-d')
        if d.amount_missing():
            tail = f"closed {date_str}, store count not set — see anomaly note"
        else:
            tail = f"closed {date_str}, {d.amount:,} store{'s' if d.amount != 1 else ''}"
        lines.append(("●", f"<b>{name}</b> → Active ({tail})", GOOD))

    for d in M["in_lab_new_this_week"][:2]:
        if d.amount > 0:
            tail = f"{d.amount:,} stores"
        else:
            tail = "store count to be set"
        date_str = fmt_date(d.entered_current, '%b %-d') if d.entered_current else ""
        lines.append(("↑",
                      f"{short_name(d.name, 32)} → In Lab ({tail}, entered {date_str})",
                      INK))

    n_new = len(M["new_deals_this_week"])
    if n_new > 0:
        names = ", ".join(short_name(d.name, 18) for d in M["new_deals_this_week"][:3])
        lines.append(("+",
                      f"<b>{n_new} new deal{'s' if n_new != 1 else ''}</b> entered pipeline this week ({names})",
                      INK))

    if not lines:
        lines.append(("—", "No notable movement this week.", INK_FAINT))
    return lines[:6]


def format_required_next_week(M):
    """Action-focused list: largest stalled commitments to unblock.

    The column is 'Required next week to bend the curve' — items here must
    plausibly bend the trajectory toward 25K. Data-hygiene items (Amount
    not logged, Blocked Reason missing) live on Page 3 instead.

    Format: top 4 stalled groups by store count, plus a 5th summary line
    rolling up everything else stalled 60+ days.
    """
    lines = []
    top4 = M["top4_stalled_by_stores"]
    if not top4:
        lines.append(("✓", "No stalled commitments 60+ days.", GOOD))
        return lines

    # First bullet — biggest unblock — uses 'Resolve' verb and is more verbose
    first = top4[0]
    blocker = first["reason"] if first["reason"] != "—" else "no blocker logged"
    lines.append(("→",
                  f"Resolve <b>{short_name(first['name'].split(' · ')[0], 28)}</b> — "
                  f"{first['stores']:,} stores, {first['days']}d, {blocker}",
                  INK))

    # Bullets 2-4 — 'Unstick' verb, same shape
    for r in top4[1:4]:
        blocker = r["reason"] if r["reason"] != "—" else "no blocker logged"
        lines.append(("→",
                      f"Unstick <b>{short_name(r['name'].split(' · ')[0], 28)}</b> — "
                      f"{r['stores']:,} stores, {r['days']}d, {blocker}",
                      INK))

    # 5th bullet — summary of all remaining stalled
    remaining_stores = M["stalled_remaining_stores"]
    remaining_deals  = M["stalled_remaining_deals"]
    if remaining_deals > 0:
        lines.append(("→",
                      f"<b>Remaining stalled</b> — {remaining_stores:,} stores across "
                      f"{remaining_deals} smaller deals (address opportunistically)",
                      INK_SOFT))

    return lines


def table_style(bg_data_rows=None):
    style = [
        ('FONT',      (0,0), (-1,0), 'Helvetica-Bold', 7.5),
        ('FONT',      (0,1), (-1,-1), 'Helvetica',     8.5),
        ('TEXTCOLOR', (0,0), (-1,0), INK_FAINT),
        ('TEXTCOLOR', (0,1), (-1,-1), INK),
        ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
        ('LINEBELOW', (0,1), (-1,-2), 0.2, RULE_SOFT),
        ('ALIGN',     (-1,0), (-1,-1), 'RIGHT'),
        # 5pt LEFT, 4pt RIGHT — gives a bit more breathing room between columns
        # without making tables feel sparse. The asymmetry helps the eye read
        # column boundaries.
        ('LEFTPADDING',(0,0),(-1,-1),5),
        ('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),3),
        ('BOTTOMPADDING',(0,0),(-1,-1),3),
    ]
    if bg_data_rows is not None:
        style.append(('BACKGROUND', (0,1), (-1,-1), bg_data_rows))
    return TableStyle(style)


# ============================================================
# PAGE 2 — DEEP DIVE
# ============================================================
def page2(c, M, payload):
    asof_label = M["asof_label"]
    pulled_at = fmt_date((parse_dt(M["pulled_at"]) or M["asof"]), "%b %-d, %Y")
    draw_header(c, asof_label, pulled_at)
    y = PAGE_H - 0.95*inch

    pill_h = 0.50*inch
    pill_gap = 0.15*inch
    pill_w = (CONTENT_W - pill_gap) / 2

    # Trajectory pill — cream + navy
    c.setFillColor(BG_BAND); c.setStrokeColor(BG_BAND); c.setLineWidth(0)
    c.rect(MARGIN_L, y - pill_h, pill_w, pill_h, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 9); c.setFillColor(NAVY)
    c.drawString(MARGIN_L + 0.1*inch, y - 0.18*inch,
                 f"■ Tracking to {M['projected_total']:,} · Goal {GOAL:,}")
    c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
    if M["pace_gap_multiple"]:
        pill1_detail = (f"{M['pace_gap_multiple']}× pace gap "
                        f"({M['pace_per_month']}/mo vs {M['required_pace']:,}/mo required)")
    else:
        pill1_detail = f"Pace: {M['pace_per_month']}/mo · Required: {M['required_pace']:,}/mo"
    c.drawString(MARGIN_L + 0.1*inch, y - 0.34*inch, pill1_detail)

    # Longest-stuck pill — pink + coral
    px = MARGIN_L + pill_w + pill_gap
    c.setFillColor(BG_STALLED); c.setStrokeColor(BG_STALLED); c.setLineWidth(0)
    c.rect(px, y - pill_h, pill_w, pill_h, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 9); c.setFillColor(ACCENT_DARK)
    if M["top5_stalled"]:
        top = M["top5_stalled"][0]
        stores_str = f"{top['stores']:,} store" + ("s" if top["stores"] != 1 else "")
        title = f"■ Longest stuck: {short_name(top['name'], 32)} ({stores_str}, {top['days']}d)"
        c.drawString(px + 0.1*inch, y - 0.18*inch, title)
        c.setFont("Helvetica", 8.5); c.setFillColor(INK_SOFT)
        # Use stage_display (registry) — not raw HubSpot label, which can
        # have double-spaces from how stages are stored in HubSpot.
        stage_label = stage_display(top["stage"], payload)
        sub_parts = [stage_label]
        if top["reason"] != "—":
            sub_parts.append(top["reason"])
        sub = " · ".join(sub_parts)
        c.drawString(px + 0.1*inch, y - 0.34*inch, sub[:78])
    else:
        c.drawString(px + 0.1*inch, y - 0.18*inch, "■ No stalled deals 60+ days")

    y -= pill_h + 0.20*inch

    section_label(c, MARGIN_L, y, "Pipeline at a Glance — Path to 25K by Dec 31, 2026")
    y -= 0.08*inch
    bar_path = "/tmp/pipeline_bar.png"; render_pipeline_bar(M, bar_path)
    bar_h = 1.10*inch
    c.drawImage(bar_path, MARGIN_L, y - bar_h, width=CONTENT_W, height=bar_h,
                preserveAspectRatio=True, mask='auto')
    y -= bar_h + 0.10*inch

    section_label(c, MARGIN_L, y, "Current Trend vs. Required Trend")
    y -= 0.10*inch
    trend_path = "/tmp/trend_chart.png"; render_trend_chart(M, trend_path)
    trend_h = 1.45*inch
    c.drawImage(trend_path, MARGIN_L, y - trend_h, width=CONTENT_W, height=trend_h,
                preserveAspectRatio=True, mask='auto')
    y -= trend_h + 0.14*inch

    section_label(c, MARGIN_L, y, "Predictive Signals — 8 Metrics Tracking Whether Trajectory Is Bending")
    y -= 0.10*inch
    signals = build_predictive_signals(M)
    sig_data = [["METRIC", "VALUE", "INTERPRETATION"]] + signals
    sig_tbl = Table(sig_data, colWidths=[1.3*inch, 1.55*inch, 4.65*inch])
    sig_tbl.setStyle(TableStyle([
        ('FONT',      (0,0), (-1,0), 'Helvetica-Bold', 7),
        ('FONT',      (0,1), (-1,-1), 'Helvetica',     7.2),
        ('FONT',      (0,1), (0,-1), 'Helvetica-Bold', 7.2),
        ('FONT',      (1,1), (1,-1), 'Helvetica-Bold', 7.2),
        ('TEXTCOLOR', (0,0), (-1,0), INK_FAINT),
        ('TEXTCOLOR', (0,1), (0,-1), INK),
        ('TEXTCOLOR', (1,1), (1,-1), NAVY),
        ('TEXTCOLOR', (2,1), (2,-1), INK_SOFT),
        ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
        ('LINEBELOW', (0,1), (-1,-2), 0.2, RULE_SOFT),
        ('LEFTPADDING',(0,0),(-1,-1),4),
        ('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),2),
        ('BOTTOMPADDING',(0,0),(-1,-1),2),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    sig_tbl.wrapOn(c, CONTENT_W, 4*inch)
    th = sig_tbl._height; sig_tbl.drawOn(c, MARGIN_L, y - th)
    y -= th + 0.16*inch

    sw_total = M["sw_total"]; sw_cat = M["sw_categorized"]; sw_unc = M["sw_uncategorized"]
    section_label(c, MARGIN_L, y,
                  f"Software Dependency Exposure — {sw_cat:,} categorized of {sw_total:,} Awaiting Software")
    y -= 0.12*inch
    c.setFont("Helvetica-Oblique", 7.5); c.setFillColor(INK_FAINT)
    c.drawString(MARGIN_L, y, f"{sw_unc:,} stores uncategorized — no Blocked Reason set (see Signal 5)")
    y -= 0.14*inch

    # Horizontal stacked bar — replaces the pie chart
    vbar_path = "/tmp/vendor_bar.png"; render_vendor_bar(M, vbar_path)
    vbar_h = 0.85*inch
    c.drawImage(vbar_path, MARGIN_L, y - vbar_h, width=CONTENT_W, height=vbar_h,
                preserveAspectRatio=True, mask='auto')
    y -= vbar_h + 0.14*inch

    # Full-width vendor table below
    vendor_rows = [["VENDOR", "STORES", "%", "RETAILERS"]]
    sorted_vendors = sorted(M["vendor_totals"].items(), key=lambda kv: -kv[1])
    for vname, vstores in sorted_vendors:
        pct = round(100 * vstores / max(1, sw_cat))
        retailers = M["vendor_retailers"][vname]
        retailers_str = " · ".join(f"{nm} {amt}" for nm, amt in retailers[:6])
        vendor_rows.append([vname, f"{vstores:,}", f"{pct}%", retailers_str[:120]])
    if len(vendor_rows) == 1:
        vendor_rows.append(["(no categorized SW deals)", "—", "—", "—"])

    vendor_tbl = Table(vendor_rows, colWidths=[1.6*inch, 0.7*inch, 0.5*inch, CONTENT_W - 2.8*inch])
    vendor_tbl.setStyle(TableStyle([
        ('FONT',      (0,0), (-1,0), 'Helvetica-Bold', 7),
        ('FONT',      (0,1), (-1,-1), 'Helvetica',     7.5),
        ('FONT',      (0,1), (0,-1), 'Helvetica-Bold', 7.5),
        ('FONT',      (1,1), (1,-1), 'Helvetica-Bold', 7.5),
        ('TEXTCOLOR', (0,0), (-1,0), INK_FAINT),
        ('TEXTCOLOR', (0,1), (-1,-1), INK),
        ('TEXTCOLOR', (1,1), (1,-1), NAVY),
        ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
        ('LINEBELOW', (0,1), (-1,-2), 0.2, RULE_SOFT),
        ('ALIGN',     (1,0), (2,-1), 'RIGHT'),
        ('LEFTPADDING',(0,0),(-1,-1),4),
        ('RIGHTPADDING',(0,0),(-1,-1),4),
        ('TOPPADDING',(0,0),(-1,-1),3),
        ('BOTTOMPADDING',(0,0),(-1,-1),3),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    vendor_tbl.wrapOn(c, CONTENT_W, 2*inch)
    th = vendor_tbl._height
    vendor_tbl.drawOn(c, MARGIN_L, y - th)
    y -= th + 0.20*inch

    # ---- STAGE-BY-STAGE FUNNEL TABLE (2 columns × 7 rows) ----
    section_label(c, MARGIN_L, y, "Stage-by-Stage Funnel — All Stages with Deals")
    y -= 0.10*inch
    c.setFont("Helvetica-Oblique", 7); c.setFillColor(INK_FAINT)
    c.drawString(MARGIN_L, y,
                 "Median days flagged red where unusually high for the stage")
    y -= 0.12*inch

    rows = M["funnel_breakdown"]

    # Stages where we'd consider median days "unusually high".
    # Operational stages should turn over fast (target: under 60 days).
    # Top-of-funnel stages ought to turn over within ~30-45 days too;
    # if Qualified or Get Started Form has a 100+ day median, that's stuck.
    def is_high_median(label, median):
        if median is None:
            return False
        # Threshold per stage class — labels here are the short forms
        # produced by stage_short() (used in the funnel table).
        if label in ("Leads", "Unqualified"):
            return False  # leads can sit a while; don't flag
        if label in ("Qualified", "Get Started Form", "Setup Guide", "Legals Signed"):
            return median > 90
        if label in ("In Lab",):
            return median > 90
        if label in ("Await. SW", "Await. Act.", "Await. Trans", "Onboarding"):
            return median > 60
        return False

    cell = ParagraphStyle('cell', fontName='Helvetica', fontSize=7,
                          textColor=INK, leading=8.2)
    cell_red = ParagraphStyle('cell_red', fontName='Helvetica', fontSize=7,
                               textColor=ACCENT_DARK, leading=8.2)
    cell_lbl = ParagraphStyle('cell_lbl', fontName='Helvetica', fontSize=7,
                              textColor=INK, leading=8.2)

    def make_row(r):
        med = r["median_days"]
        if med is None:
            med_txt = "—"
            med_para = Paragraph(med_txt, cell)
        else:
            med_txt = f"{med}"
            med_para = Paragraph(med_txt, cell_red if is_high_median(r["label"], med) else cell)
        return [
            Paragraph(r["label"], cell_lbl),
            Paragraph(f"{r['deals']:,}", cell),
            Paragraph(f"{r['stores']:,}", cell),
            med_para,
        ]

    # Split into two halves — funnel order preserved within each
    n_total = len(rows)
    half = (n_total + 1) // 2  # ceil division so left col holds the extra
    left_rows  = rows[:half]
    right_rows = rows[half:]

    header = [Paragraph(t, ParagraphStyle('h', fontName='Helvetica-Bold',
                        fontSize=7, textColor=INK_FAINT, leading=8))
              for t in ("STAGE", "DEALS", "STORES", "DAYS")]
    left_data  = [header] + [make_row(r) for r in left_rows]
    right_data = [header] + [make_row(r) for r in right_rows]

    col_w = (CONTENT_W - 0.20*inch) / 2
    inner_widths = [col_w * 0.45, col_w * 0.18, col_w * 0.20, col_w * 0.17]

    common_style = TableStyle([
        ('FONT',      (0,0), (-1,0), 'Helvetica-Bold', 7),
        ('TEXTCOLOR', (0,0), (-1,0), INK_FAINT),
        ('LINEBELOW', (0,0), (-1,0), 0.5, RULE),
        ('LINEBELOW', (0,1), (-1,-2), 0.2, RULE_SOFT),
        ('ALIGN',     (1,0), (-1,-1), 'RIGHT'),
        ('LEFTPADDING',(0,0),(-1,-1),3),
        ('RIGHTPADDING',(0,0),(-1,-1),3),
        ('TOPPADDING',(0,0),(-1,-1),1.5),
        ('BOTTOMPADDING',(0,0),(-1,-1),1.5),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ])

    left_tbl  = Table(left_data,  colWidths=inner_widths)
    right_tbl = Table(right_data, colWidths=inner_widths)
    left_tbl.setStyle(common_style)
    right_tbl.setStyle(common_style)

    left_tbl.wrapOn(c, col_w, 4*inch)
    right_tbl.wrapOn(c, col_w, 4*inch)
    th_left  = left_tbl._height
    th_right = right_tbl._height
    th_max = max(th_left, th_right)

    left_tbl.drawOn(c, MARGIN_L, y - th_left)
    right_tbl.drawOn(c, MARGIN_L + col_w + 0.20*inch, y - th_right)
    y -= th_max + 0.06*inch

    draw_footer(c, 2, 3, "Deep Dive",
                f"Source: HubSpot live pull, {pulled_at}")


def build_predictive_signals(M):
    signals = []

    signals.append([
        "1. Pace gap",
        f"{M['pace_per_month']}/mo vs {M['required_pace']:,}/mo",
        f"{M['pace_gap_multiple']}× gap vs Dec 31 target of {GOAL:,}. Most important number on this report. Target: close weekly."
    ])

    signals.append([
        "2. Stalled deals",
        f"{M['stalled_30d_count']} deals · {M['stalled_30d_stores']:,} stores",
        "Deals not advancing 30+ days (excludes top-of-funnel). 30–59d nudge zone, 60+d urgent. Target: trending down."
    ])

    signals.append([
        "3. Fwd calendar",
        f"{M['fwd_calendar_count']} deals / next 14 days",
        f"Healthy pipeline = {M['fwd_calendar_target']}+. Below target indicates unplanned or unlogged work. Target: {M['fwd_calendar_target']}+."
    ])

    signals.append([
        "4. Lab median",
        f"~{M['lab_median_days']} days",
        "Median days In Lab before advancing. Halving this doubles annual pace. Target: under 30 days."
    ])

    signals.append([
        "5. SW hygiene",
        f"{M['sw_uncategorized_pct']}% uncategorized",
        f"{M['sw_uncategorized']:,} stores Awaiting Software with no Blocked Reason. Operationally opaque. Target: under 20%."
    ])

    n_active = M["active_deal_count"]
    avg_per_deal = round(M['active_stores'] / max(1, n_active), 1)
    signals.append([
        "6. Active count",
        f"{M['active_stores']:,} stores · {n_active} deals",
        f"Avg {avg_per_deal} stores/deal. Verify large outliers via Page 3 anomalies."
    ])

    committed = M['active_stores'] + M['in_progress_stores']
    if committed > 0:
        conv = round(100 * M['active_stores'] / committed)
        signals.append([
            "7. Funnel conv.",
            f"~{conv}% / {M['pace_per_month']} per mo",
            f"Of {committed:,} committed stores, % converted to Active. Doubling = doubling annual pace."
        ])

    new_count = len(M['new_deals_this_week'])
    signals.append([
        "8. New entries",
        f"{new_count} new deals this week",
        "Top-of-funnel velocity. Sustained low intake will starve the pipeline downstream."
    ])
    return signals


# ============================================================
# PAGE 3 — DATA ANOMALIES
# ============================================================
def page3(c, M, payload):
    asof_label = M["asof_label"]
    pulled_at = fmt_date((parse_dt(M["pulled_at"]) or M["asof"]), "%b %-d, %Y")
    draw_header(c, asof_label, pulled_at, page_subtitle="Data Quality & Action Items")

    y = PAGE_H - 1.10*inch

    intro_h = 0.50*inch
    c.setFillColor(BG_INFO); c.setStrokeColor(BORDER_INFO); c.setLineWidth(0.5)
    c.rect(MARGIN_L, y - intro_h, CONTENT_W, intro_h, fill=1, stroke=0)
    c.setFillColor(BORDER_INFO)
    c.rect(MARGIN_L, y - intro_h, 0.05*inch, intro_h, fill=1, stroke=0)

    c.setFont("Helvetica", 8.5); c.setFillColor(INK)
    blurb = (f"This page lists data anomalies detected in the {pulled_at} HubSpot pull. "
             f"Items are prioritized by impact on report accuracy. Each row identifies the "
             f"records, the issue, and the action — fixing them before the next pull lets "
             f"the report run cleanly without manual reconciliation.")
    txt_x = MARGIN_L + 0.18*inch
    yy = y - 0.16*inch
    for line in simpleSplit(blurb, "Helvetica", 8.5, CONTENT_W - 0.3*inch):
        c.drawString(txt_x, yy, line); yy -= 0.12*inch

    y -= intro_h + 0.18*inch

    A = M["anomalies"]

    # ---- PRIORITY SCORECARD ----
    section_label(c, MARGIN_L, y, "Priority Scorecard — All Anomalies This Week", color=NAVY)
    y -= 0.10*inch

    pri_rows = [["ANOMALY", "DETAIL", "PRIORITY"]]

    # Headline anomaly: gap between actual store count and deal Amount sum.
    # Only meaningful if we have store data to compare against.
    if M["has_store_data"]:
        deal_legit = A["active_legit_sum"]
        store_active = M["active_stores_real"]
        gap = store_active - deal_legit
        if abs(gap) > 50:   # only flag if meaningful
            pri_rows.append([
                "Active count: Stores ↔ Deal Amount mismatch",
                (f"Stores object: {store_active:,} active · "
                 f"Deal Amount sum (legit): {deal_legit:,} · "
                 f"Gap: {gap:+,} stores"),
                "CRITICAL"
            ])
    elif A["test_in_active"] or A["closedwon_no_amount"]:
        # No store data — fall back to old framing
        details = []
        if A["test_in_active"]:
            details.append(f"{A['test_in_active_stores']:,} stores from test record(s)")
        if A["closedwon_no_amount"]:
            details.append(f"{len(A['closedwon_no_amount'])} closedwon with no Amount")
        detail_str = "; ".join(details)
        pri_rows.append([
            "Active store count: Amount-field accuracy",
            f"Sum {M['active_stores']:,} (legit ~{A['active_legit_sum']:,}). {detail_str}.",
            "CRITICAL"
        ])

    if A["test_in_active"]:
        pri_rows.append([
            "Test record(s) in Closed Won",
            f"{len(A['test_in_active'])} record · {A['test_in_active_stores']:,} stores · primary inflation source",
            "CRITICAL"
        ])

    if A["test_in_pipeline"]:
        pri_rows.append([
            "Test/junk deal names in live pipeline",
            f"{len(A['test_in_pipeline'])} records inflate stage counts",
            "HIGH"
        ])

    if A["pipeline_no_amount"]:
        pri_rows.append([
            "Pipeline deals missing Amount/store count",
            f"{len(A['pipeline_no_amount'])} deals (mid/late funnel) — unknown store impact",
            "HIGH"
        ])

    if A["closedwon_no_amount"]:
        names = ", ".join(short_name(d.name, 18) for d in A["closedwon_no_amount"][:2])
        pri_rows.append([
            "Closed Won deals with no Amount field",
            f"{len(A['closedwon_no_amount'])} deals: {names}",
            "HIGH"
        ])

    if A["zero_amount"]:
        names = ", ".join(short_name(d.name, 16) for d in A["zero_amount"][:3])
        pri_rows.append([
            "Deals set to $0 in active pipeline",
            f"{len(A['zero_amount'])} deals: {names}",
            "MEDIUM"
        ])

    if A["no_owner"]:
        names = ", ".join(short_name(d.name, 16) for d in A["no_owner"][:3])
        pri_rows.append([
            "Deals with no owner assigned",
            f"{len(A['no_owner'])} deals: {names}",
            "MEDIUM"
        ])

    if A["sw_no_reason"]:
        pri_rows.append([
            "Awaiting Software deals with no Blocked Reason",
            f"{len(A['sw_no_reason'])} deals · {A['sw_no_reason_stores']:,} stores — opaque pipeline band",
            "MEDIUM"
        ])

    if A["stale_early"]:
        pri_rows.append([
            "Stale early-funnel prospects (90+ days)",
            f"{len(A['stale_early'])} deals in Get Started/Qualified/Setup Guide stages",
            "MEDIUM"
        ])

    if A["duplicates"]:
        names = ", ".join(short_name(n, 18) for n, _ in A["duplicates"][:3])
        pri_rows.append([
            "Duplicate deal names in pipeline",
            f"{len(A['duplicates'])} duplicate-name groups: {names}",
            "LOW"
        ])

    if len(pri_rows) == 1:
        pri_rows.append(["(no anomalies detected)", "—", "—"])

    pri_tbl = Table(pri_rows, colWidths=[2.7*inch, 4.1*inch, 0.7*inch])
    pri_tbl.setStyle(navy_table_style(rows=pri_rows, priority_col=2))
    pri_tbl.wrapOn(c, CONTENT_W, 3*inch)
    th = pri_tbl._height; pri_tbl.drawOn(c, MARGIN_L, y - th)
    y -= th + 0.16*inch

    # ---- ANOMALY 1 detail: Active store accounting ----
    if A["test_in_active"] or A["closedwon_no_amount"] or M["has_store_data"]:
        title_a1 = ("Anomaly 1 — Active Count: Stores vs. Deal Amounts"
                    if M["has_store_data"]
                    else "Anomaly 1 — Active Store Count: Deal Amounts vs. Records")
        section_label(c, MARGIN_L, y, title_a1, color=NAVY)
        y -= 0.10*inch
        cell = ParagraphStyle('cell', fontName='Helvetica', fontSize=7,
                              textColor=INK, leading=8.5)
        cell_red = ParagraphStyle('cell_red', fontName='Helvetica', fontSize=7,
                                   textColor=ACCENT_DARK, leading=8.5)
        a1_rows = [["ISSUE", "DETAIL", "IMPACT", "ACTION"]]

        if M["has_store_data"]:
            store_active = M["active_stores_real"]
            deal_legit   = A["active_legit_sum"]
            gap = store_active - deal_legit
            a1_rows.append([
                Paragraph("Stores ↔ Deals reconciliation", cell),
                Paragraph(f"HubSpot Stores: <b>{store_active:,}</b> active. "
                          f"Closed-won deal Amount sum (excl. test records): "
                          f"<b>{deal_legit:,}</b>. Gap: <b>{gap:+,}</b>.", cell),
                Paragraph(f"Report headline {store_active:,} matches Stores. "
                          f"But ~{abs(gap):,} stores are activated without their "
                          f"deal Amount being updated to match.", cell),
                Paragraph("Update Amount on closed-won deals so deal-pipeline "
                          "metrics match the Stores object.", cell),
            ])
        else:
            a1_rows.append([
                Paragraph("Closed-won amount sum", cell),
                Paragraph(f"closedwon Amount field sums to <b>{M['active_stores']:,}</b>. "
                          f"Excluding test records: ~<b>{A['active_legit_sum']:,}</b>.", cell),
                Paragraph(f"Report shows {M['active_stores']:,}* with caveat. "
                          f"Auto-report relies on the sum being clean.", cell),
                Paragraph("Populate Amount on every closedwon deal with verified store count.", cell),
            ])

        for d in A["test_in_active"]:
            a1_rows.append([
                Paragraph(f"<b>{short_name(d.name, 28)}</b> in Closed Won", cell_red),
                Paragraph(f"Test record closed Won with Amount = <b>{d.amount:,}</b>. "
                          f"Primary driver of inflation.", cell),
                Paragraph(f"Inflates active count by {d.amount:,}. Should not exist in production.", cell),
                Paragraph("Delete or archive. Do not count toward active stores.", cell),
            ])

        if A["closedwon_no_amount"]:
            names_list = []
            for d in A["closedwon_no_amount"]:
                cd = d.stage_entries.get("closedwon") or d.closed
                cd_str = fmt_date(cd, "%b %-d %Y") if cd else "?"
                names_list.append(f"<b>{short_name(d.name, 22)}</b> ({cd_str})")
            names_html = "; ".join(names_list)
            a1_rows.append([
                Paragraph(f"{len(A['closedwon_no_amount'])} closedwon with no Amount field", cell),
                Paragraph(names_html, cell),
                Paragraph("Stores completely uncounted in all reporting.", cell),
                Paragraph("Set Amount on each. Confirm counts with the deal owner.", cell),
            ])

        a1_tbl = Table(a1_rows, colWidths=[1.4*inch, 2.2*inch, 1.9*inch, 2.0*inch])
        a1_tbl.setStyle(navy_table_style(rows=a1_rows))
        a1_tbl.wrapOn(c, CONTENT_W, 4*inch)
        th = a1_tbl._height
        a1_tbl.drawOn(c, MARGIN_L, y - th)
        y -= th + 0.16*inch

    # ---- ANOMALY 2 detail: Pipeline deals missing Amount ----
    if A["pipeline_no_amount"] and y > MARGIN_B + 1.6*inch:
        n_total = len(A["pipeline_no_amount"])
        show_top = min(14, n_total)
        section_label(c, MARGIN_L, y,
                      f"Anomaly 2 — Pipeline Deals Missing Store Count ({n_total} total — top {show_top} shown)",
                      color=NAVY)
        y -= 0.10*inch
        cell = ParagraphStyle('cell', fontName='Helvetica', fontSize=7,
                              textColor=INK, leading=8.2)
        cell_red = ParagraphStyle('cell_red', fontName='Helvetica', fontSize=7,
                                   textColor=ACCENT_DARK, leading=8.2)
        a2_rows = [["DEAL", "STAGE", "CREATED", "ISSUE"]]

        sorted_pl = sorted(A["pipeline_no_amount"],
                           key=lambda d: d.created or datetime(1970,1,1,tzinfo=timezone.utc),
                           reverse=True)
        for d in sorted_pl[:show_top]:
            issue = "No amount"
            issue_cell = cell
            name_cell = cell
            if d.is_test_record():
                issue = "Test record in live pipeline — remove"
                issue_cell = cell_red
                name_cell = cell_red
            elif "qrjwxjqsbuxciwmljofcd" in d.name.lower():
                issue = "Junk system name — likely duplicate"
                issue_cell = cell_red
                name_cell = cell_red
            elif not d.owner:
                issue = "No amount + no owner assigned"
                issue_cell = cell_red

            stage_label = short_stage(d.stage, payload["stage_labels"], payload)
            created = fmt_date(d.created, "%b %-d") if d.created else "—"

            a2_rows.append([
                Paragraph(short_name(d.name, 30), name_cell),
                Paragraph(stage_label, cell),
                Paragraph(created, cell),
                Paragraph(issue, issue_cell),
            ])

        a2_tbl = Table(a2_rows, colWidths=[2.5*inch, 1.0*inch, 0.85*inch, 3.15*inch])
        a2_tbl.setStyle(navy_table_style(rows=a2_rows))
        a2_tbl.wrapOn(c, CONTENT_W, 4*inch)
        th = a2_tbl._height
        a2_tbl.drawOn(c, MARGIN_L, y - th)
        y -= th + 0.16*inch

    # ---- ANOMALY 3: Deals with no owner ----
    if A["no_owner"] and y > MARGIN_B + 1.5*inch:
        section_label(c, MARGIN_L, y,
                      f"Anomaly 3 — Deals With No Owner Assigned ({len(A['no_owner'])})",
                      color=NAVY)
        y -= 0.10*inch
        cell = ParagraphStyle('cell', fontName='Helvetica', fontSize=7,
                              textColor=INK, leading=8.2)
        a3_rows = [["DEAL", "STAGE", "AGE", "ACTION"]]
        for d in A["no_owner"][:8]:
            stage = short_stage(d.stage, payload["stage_labels"], payload)
            age = d.created.strftime("%b %Y") if d.created else "—"
            note = "Demo unit — archive or move to test" if d.is_test_record() else "Assign owner or close"
            a3_rows.append([
                Paragraph(short_name(d.name, 30), cell),
                Paragraph(stage, cell),
                Paragraph(age, cell),
                Paragraph(note, cell),
            ])
        a3_tbl = Table(a3_rows, colWidths=[2.0*inch, 1.0*inch, 0.9*inch, 3.6*inch])
        a3_tbl.setStyle(navy_table_style(rows=a3_rows))
        a3_tbl.wrapOn(c, CONTENT_W, 2*inch)
        th = a3_tbl._height
        if y - th >= MARGIN_B + 0.5*inch:
            a3_tbl.drawOn(c, MARGIN_L, y - th)
            y -= th + 0.12*inch

    draw_footer(c, 3, 3, "Data Anomalies",
                "For Data Team — action before next weekly pull")


def navy_table_style(rows, priority_col=None):
    style = [
        ('BACKGROUND', (0,0), (-1,0), NAVY),
        ('TEXTCOLOR',  (0,0), (-1,0), NAVY_TEXT),
        ('FONT',       (0,0), (-1,0), 'Helvetica-Bold', 7.5),
        ('TOPPADDING', (0,0), (-1,0), 5),
        ('BOTTOMPADDING', (0,0), (-1,0), 5),
        ('FONT',      (0,1), (-1,-1), 'Helvetica',     7.5),
        ('TEXTCOLOR', (0,1), (-1,-1), INK),
        ('LINEBELOW', (0,1), (-1,-2), 0.2, RULE_SOFT),
        ('LINEBELOW', (0,-1), (-1,-1), 0.2, RULE_SOFT),
        ('LEFTPADDING',(0,0),(-1,-1),5),
        ('RIGHTPADDING',(0,0),(-1,-1),5),
        ('TOPPADDING',(0,1),(-1,-1),3.5),
        ('BOTTOMPADDING',(0,1),(-1,-1),3.5),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]
    if priority_col is not None:
        style.append(('FONT', (priority_col,1), (priority_col,-1), 'Helvetica-Bold', 7.5))
        style.append(('ALIGN', (priority_col,0), (priority_col,-1), 'CENTER'))
        for i, row in enumerate(rows[1:], start=1):
            priority = row[priority_col] if len(row) > priority_col else ""
            if priority == "CRITICAL":
                style.append(('TEXTCOLOR', (priority_col,i), (priority_col,i), ACCENT_DARK))
            elif priority == "HIGH":
                style.append(('TEXTCOLOR', (priority_col,i), (priority_col,i), HexColor("#a06010")))
            elif priority == "MEDIUM":
                style.append(('TEXTCOLOR', (priority_col,i), (priority_col,i), HexColor("#666666")))
            else:
                style.append(('TEXTCOLOR', (priority_col,i), (priority_col,i), INK_FAINT))
    return TableStyle(style)


# ============================================================
# DRIVER
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Generate the TruAge Activation Report PDF")
    parser.add_argument("--input",  default="hubspot_pull.json")
    parser.add_argument("--output", default=None)
    parser.add_argument("--date",   default=None)
    args = parser.parse_args()

    payload, deals = load_data(args.input)
    if args.date:
        asof = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    elif payload.get("report_date"):
        asof = datetime.fromisoformat(payload["report_date"]).replace(tzinfo=timezone.utc)
    else:
        asof = datetime.now(timezone.utc)

    M = compute_metrics(payload, deals, asof)

    out = args.output or f"TruAge_Activation_Report_{asof.strftime('%Y-%m-%d')}.pdf"
    c = canvas.Canvas(out, pagesize=letter)
    c.setTitle("TruAge Activation Report")
    c.setAuthor("HubSpot Live Pull")
    c.setSubject(f"Week ending {M['asof_label']}")

    page1(c, M, payload); c.showPage()
    page2(c, M, payload); c.showPage()
    page3(c, M, payload); c.showPage()
    c.save()
    print(f"Saved: {out}")
    print(f"  Active: {M['active_stores']:,} ({M['active_deal_count']} deals)")
    print(f"  In Lab: {M['in_lab_stores']:,} | Await SW: {M['sw_stores']:,} | "
          f"Await Act: {M['act_stores']:,} | Await Trans: {M['trans_stores']:,}")
    print(f"  Stalled 30+ (active funnel): {M['stalled_30d_count']} deals, "
          f"{M['stalled_30d_stores']:,} stores "
          f"(30-59: {M['stalled_buckets']['30_59']['stores']:,}, "
          f"60-89: {M['stalled_buckets']['60_89']['stores']:,}, "
          f"90+: {M['stalled_buckets']['90p']['stores']:,})")
    print(f"  Pace: {M['pace_per_month']}/mo · Required: {M['required_pace']:,}/mo")
    a = M["anomalies"]
    print(f"  Anomalies: test-active={len(a['test_in_active'])}, "
          f"cw-no-amt={len(a['closedwon_no_amount'])}, "
          f"pipe-no-amt={len(a['pipeline_no_amount'])}, "
          f"test-pipe={len(a['test_in_pipeline'])}, "
          f"no-owner={len(a['no_owner'])}, "
          f"sw-no-reason={len(a['sw_no_reason'])}, "
          f"dups={len(a['duplicates'])}, "
          f"stale-early={len(a['stale_early'])}")


if __name__ == "__main__":
    main()
