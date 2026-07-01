"""
TruAge Activation Report — HTML Generator
==========================================

Reads:  hubspot_pull.json
Writes: TruAge_Activation_Report_<DATE>.html

Self-contained single-file HTML output — open in any browser, print to PDF,
send as email body, or attach as a file.

SAME DATA PRINCIPLES as generate_report.py:
  1. The 25,000 store goal by Dec 31, 2026 is the ONLY hardcoded number.
  2. Tone is forthcoming, not accusatory. No exclamation points.
  3. Page 3 is a complete anomaly punch list.

SCAFFOLDING NOTE (future):
  Data tables are rendered with data-id="<deal_id>" attributes on rows
  so future click-through linking to HubSpot records is trivial to add.
  Each table row with a HubSpot deal ID is already tagged.

To run:
    python generate_report_html.py                      # uses today
    python generate_report_html.py --date 2026-05-11    # specific week
    python generate_report_html.py --input mypull.json --output myreport.html
"""
from __future__ import annotations
import argparse
import json
import math
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ============================================================
# CONFIG — the only hardcoded numbers
# ============================================================
GOAL          = 25_000
GOAL_DATE_STR = "Dec 31, 2026"
GOAL_DATE     = datetime(2026, 12, 31, tzinfo=timezone.utc)
HUBSPOT_BASE  = "https://app.hubspot.com/contacts/46513369/record/0-3"

TEST_SUBSTRING_PATTERNS = [
    "thinksys", "qrjwxjqsbuxciwmljofcd", "demo unit",
    "homeless not helpless", "muhammad hassan", "mendietaaaa", "bunny palace",
]
TEST_EXACT_NAMES = {
    "tester", "self employed", "send proud", "rita", "pan", "na", "clover",
}
VENDOR_PATTERNS = {
    "Verifone (Commander)": ["verifone", "commander"],
    "NCR (Radiant)":        ["ncr", "radiant"],
    "Invenco":              ["invenco"],
    "Gilbarco":             ["gilbarco"],
}
EARLY_FUNNEL_STAGES = {
    "1346410815", "1350980982", "qualifiedtobuy",
    "appointmentscheduled", "presentationscheduled",
    "decisionmakerboughtin", "1335845536",
}

# ============================================================
# HELPERS
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

def safe_int(v):
    if v in (None, "", "null"):
        return 0
    try:
        return int(float(v))
    except Exception:
        return 0

def fmt_date(dt, pattern="%b %-d, %Y"):
    """Cross-platform strftime with no leading zeros."""
    pat = pattern.replace("%-d", "%d").replace("%-m", "%m")
    s = dt.strftime(pat)
    s = re.sub(r' 0(\d)', r' \1', s)
    s = re.sub(r'^0(\d)', r'\\1', s)
    return s

def h(text):
    """HTML-escape a string."""
    return (str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;"))

def deal_url(deal_id):
    """HubSpot deep link for a deal record (scaffolding for future click-through)."""
    return f"{HUBSPOT_BASE}/{deal_id}?utm_source=truage_report&utm_medium=html_report"

def load_data(path):
    payload = json.loads(Path(path).read_text())
    deals   = [Deal(d) for d in payload.get("deals", [])]
    return payload, deals


def short_name(name, max_len=40):
    """Trim deal name on word boundary."""
    n = name.split(" - ")[0].split(" (")[0]
    if len(n) <= max_len:
        return n
    cut = n[:max_len].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


# ============================================================
# DEAL MODEL
# ============================================================
class Deal:
    def __init__(self, raw):
        self.id             = raw.get("id", "")
        self.name           = (raw.get("dealname") or "").strip()
        self.stage          = raw.get("dealstage") or ""
        self.amount         = safe_int(raw.get("amount"))
        self.amount_raw     = raw.get("amount")
        self.owner          = raw.get("hubspot_owner_id") or ""
        self.blocked_reason = (raw.get("blocked_reason") or "").strip()
        self.created        = parse_dt(raw.get("createdate"))
        self.closed         = parse_dt(raw.get("closedate"))
        self.next_activity  = parse_dt(raw.get("notes_next_activity_date"))
        self.entered_current= parse_dt(raw.get("hs_v2_date_entered_current_stage"))
        self.stage_entries  = {
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
        return any(p in n for p in TEST_SUBSTRING_PATTERNS)

    def amount_missing(self):
        return self.amount_raw in (None, "")

    def amount_zero_explicit(self):
        return self.amount_raw not in (None, "") and safe_int(self.amount_raw) == 0


# ============================================================
# METRICS (identical logic to generate_report.py)
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

    M["payload"]     = payload
    M["stage_labels"] = stage_labels
    M["in_lab_id"]   = in_lab_id
    M["sw_id"]       = sw_id
    M["act_id"]      = act_id
    M["trans_id"]    = trans_id
    M["onb_id"]      = onb_id
    M["active_ids"]  = active_ids
    M["asof"]        = asof
    M["asof_label"]  = fmt_date(asof, "%A, %B %-d, %Y")
    M["pulled_at"]   = payload.get("pulled_at", "")

    by_stage = defaultdict(list)
    for d in deals:
        by_stage[d.stage].append(d)
    M["by_stage"] = by_stage

    def stage_sum(sid):
        return sum(d.amount for d in by_stage.get(sid, []))

    def stage_count(sid):
        return len(by_stage.get(sid, []))

    M["active_stores_deals"] = sum(stage_sum(s) for s in active_ids)
    M["active_deal_count"]   = sum(stage_count(s) for s in active_ids)
    M["in_lab_stores"]       = stage_sum(in_lab_id)
    M["sw_stores"]           = stage_sum(sw_id)
    M["act_stores"]          = stage_sum(act_id)
    M["trans_stores"]        = stage_sum(trans_id)
    M["onb_stores"]          = stage_sum(onb_id)
    M["in_progress_stores"]  = (
        M["in_lab_stores"] + M["sw_stores"] + M["act_stores"] +
        M["trans_stores"] + M["onb_stores"]
    )

    # --- Store object data ---
    stores = payload.get("stores", [])
    M["has_store_data"] = bool(stores)
    M["stores_total"]   = len(stores)

    def is_test_store(s):
        # For Store records, the is_test_data field is authoritative.
        # Do NOT apply name-pattern matching here — a store named
        # "Thinksys-Test-Store-1" with is_test_data=false is a real
        # store that ops has explicitly marked as non-test. Trust the field.
        return (s.get("is_test_data") or "").lower() == "true"

    real_stores = [s for s in stores if not is_test_store(s)]
    test_stores = [s for s in stores if is_test_store(s)]
    M["stores_test_count"] = len(test_stores)
    M["real_stores"]       = real_stores

    by_status = defaultdict(list)
    for s in real_stores:
        status = (s.get("status") or "").strip().lower().capitalize()
        by_status[status].append(s)
    M["stores_by_status"] = {k: len(v) for k, v in by_status.items()}

    def status_count(status_key):
        return M["stores_by_status"].get(status_key, 0)

    M["active_stores_real"]  = status_count("Active")
    M["pending_stores_real"] = status_count("Pending")
    M["ready_stores_real"]   = status_count("Ready")
    M["stores_total_real"]   = len(real_stores)

    M["active_stores"] = (
        M["active_stores_real"] if M["has_store_data"]
        else M["active_stores_deals"]
    )

    # --- Weekly movement ---
    week_ago = asof - timedelta(days=7)

    moved_this_week_active = [
        (d, d.stage_entries.get(s))
        for s in active_ids
        for d in by_stage.get(s, [])
        if d.stage_entries.get(s) and d.stage_entries[s] >= week_ago
    ]
    M["active_delta_week_deals"]  = sum(d.amount for d, _ in moved_this_week_active)
    M["active_delta_deal_count"]  = len(moved_this_week_active)
    M["moved_this_week_active"]   = sorted(moved_this_week_active, key=lambda x: x[1], reverse=True)

    if M["has_store_data"]:
        activated_this_week = [
            s for s in real_stores
            if parse_dt(s.get("activated_at")) and parse_dt(s.get("activated_at")) >= week_ago
        ]
        stores_activated_this_week = len(activated_this_week)
    else:
        stores_activated_this_week = M["active_delta_week_deals"]

    M["active_delta_week_stores"] = stores_activated_this_week
    M["active_delta_week"] = (
        M["active_delta_week_stores"] if M["has_store_data"]
        else M["active_delta_week_deals"]
    )

    # --- Trajectory ---
    today      = asof
    months_left = max(0, round((GOAL_DATE - today).days / 30.44))
    M["months_left"]   = months_left
    M["goal"]          = GOAL
    M["goal_date_str"] = GOAL_DATE_STR

    last30 = asof - timedelta(days=30)
    if M["has_store_data"]:
        pace_stores = sum(
            1 for s in real_stores
            if parse_dt(s.get("activated_at")) and parse_dt(s.get("activated_at")) >= last30
        )
    else:
        pace_stores = 0
    pace_deals = sum(
        d.amount for s in active_ids
        for d in by_stage.get(s, [])
        if d.stage_entries.get(s) and d.stage_entries[s] >= last30
    )
    M["pace_per_month_deals"]  = pace_deals
    M["pace_per_month_stores"] = pace_stores
    M["pace_per_month"]        = pace_stores if M["has_store_data"] else pace_deals

    # --- Prior 30-day pace (for acceleration comparison) ---
    last60 = asof - timedelta(days=60)
    if M["has_store_data"]:
        pace_prior_stores = sum(
            1 for s in real_stores
            if parse_dt(s.get("activated_at"))
            and last60 <= parse_dt(s.get("activated_at")) < last30
        )
    else:
        pace_prior_stores = 0
    pace_prior_deals = sum(
        d.amount for s in active_ids
        for d in by_stage.get(s, [])
        if d.stage_entries.get(s)
        and last60 <= d.stage_entries[s] < last30
    )
    M["pace_prior_month"]       = pace_prior_stores if M["has_store_data"] else pace_prior_deals
    M["pace_acceleration"]      = M["pace_per_month"] - M["pace_prior_month"]  # + = speeding up

    # --- Stage velocity (median days) — computed inside funnel_breakdown later ---
    # Stored as M["stage_velocity"] dict after funnel_breakdown is called

    # --- Concentration risk: % of committed pipeline from top 3 retailers ---
    retailer_stores = defaultdict(int)
    for d in deals:
        if (d.stage not in {"closedlost"}
                and d.stage not in EARLY_FUNNEL_STAGES
                and not d.is_test_record()
                and d.amount > 0):
            base = d.name.split(" - ")[0].split(" (R")[0].strip()
            retailer_stores[base] += d.amount
    top3 = sorted(retailer_stores.values(), reverse=True)[:3]
    total_committed_pipeline = sum(retailer_stores.values())
    M["top3_concentration_pct"] = round(100 * sum(top3) / max(1, total_committed_pipeline))
    M["top3_concentration_stores"] = sum(top3)
    M["retailer_store_totals"] = dict(sorted(retailer_stores.items(), key=lambda x: -x[1]))

    gap = max(0, GOAL - M["active_stores"])
    M["required_pace"]     = math.ceil(gap / months_left) if months_left > 0 else 0
    M["pace_gap_multiple"] = round(M["required_pace"] / max(1, M["pace_per_month"])) if M["pace_per_month"] else None
    M["projected_total"]   = M["active_stores"] + (M["pace_per_month"] * months_left)
    M["shortfall"]         = max(0, GOAL - M["projected_total"])

    # --- Stall analysis ---
    def is_stall_eligible(d):
        return (
            d.stage not in active_ids
            and d.stage not in {"closedwon", "closedlost"}
            and d.stage not in EARLY_FUNNEL_STAGES
            and d.days_in_stage(asof) is not None
        )

    stalled_30_all = [
        (d, d.days_in_stage(asof)) for d in deals
        if is_stall_eligible(d) and d.days_in_stage(asof) >= 30
    ]
    stalled_30_active = [(d, days) for d, days in stalled_30_all if not d.is_test_record()]
    stalled_60_active = [(d, days) for d, days in stalled_30_active if days >= 60]
    stalled_3059      = [(d, days) for d, days in stalled_30_active if 30 <= days < 60]
    stalled_6089      = [(d, days) for d, days in stalled_30_active if 60 <= days < 90]
    stalled_90p       = [(d, days) for d, days in stalled_30_active if days >= 90]

    M["stalled_30d_all"]    = stalled_30_all
    M["stalled_30d_active"] = stalled_30_active
    M["stalled_60d_active"] = stalled_60_active
    M["stalled_buckets"]    = {
        "30_59":  {"count": len(stalled_3059),  "stores": sum(d.amount for d, _ in stalled_3059)},
        "60_89":  {"count": len(stalled_6089),  "stores": sum(d.amount for d, _ in stalled_6089)},
        "90p":    {"count": len(stalled_90p),   "stores": sum(d.amount for d, _ in stalled_90p)},
    }
    M["stalled_30d_stores"] = sum(d.amount for d, _ in stalled_30_active)
    M["stalled_30d_count"]  = len(stalled_30_active)
    M["stalled_60d_stores"] = sum(d.amount for d, _ in stalled_60_active)

    # --- Build top-stall lists ---
    def group_stalled(pairs):
        groups = {}
        for d, days in pairs:
            base = re.sub(r'\s*\(R\d+/\d+\)', '', d.name)
            base = base.split(" - New Deal")[0].split(" - New Date")[0].strip()
            if base not in groups:
                groups[base] = {"base": base, "deals": [], "stores": 0, "days_min": days,
                                "days_max": days, "stages": set(), "blocked": set()}
            g = groups[base]
            g["deals"].append(d)
            g["stores"] += d.amount
            g["days_min"] = min(g["days_min"], days)
            g["days_max"] = max(g["days_max"], days)
            g["stages"].add(d.stage)
            if d.blocked_reason:
                g["blocked"].add(d.blocked_reason)
        result = list(groups.values())
        for g in result:
            g["days_str"] = (str(g["days_min"]) if g["days_min"] == g["days_max"]
                             else f"{g['days_min']}–{g['days_max']}")
            g["stage"]    = next(iter(g["stages"]))
            g["reason"]   = "; ".join(sorted(g["blocked"])) or "—"
            g["is_group"] = len(g["deals"]) > 1
            g["count"]    = len(g["deals"])
        return sorted(result, key=lambda x: (-x["days_max"], -x["stores"]))

    M["top5_stalled"]    = group_stalled(stalled_60_active)[:5]
    M["top5_stalled_60p"]   = group_stalled(stalled_60_active)[:5]
    M["top5_stalled_30_59"] = group_stalled(stalled_3059)[:5]

    # Required next week (top 4 by stores, 60+ days)
    M["top4_stalled_by_stores"] = sorted(
        group_stalled(stalled_60_active),
        key=lambda g: (-g["stores"], -g["days_max"])
    )[:4]
    remaining = group_stalled(stalled_60_active)[4:]
    M["stalled_remaining_stores"] = sum(g["stores"] for g in remaining)
    M["stalled_remaining_deals"]  = sum(g["count"] for g in remaining)

    # --- Awaiting SW ---
    sw_deals = by_stage.get(sw_id, [])
    sw_with_blocker    = [d for d in sw_deals if d.blocked_reason and not d.is_test_record()]
    sw_without_blocker = [d for d in sw_deals if not d.blocked_reason and not d.is_test_record()]
    sw_total        = sum(d.amount for d in sw_deals if not d.is_test_record())
    sw_categorized  = sum(d.amount for d in sw_with_blocker)
    sw_uncategorized= sum(d.amount for d in sw_without_blocker)

    M["sw_total"]        = sw_total
    M["sw_categorized"]  = sw_categorized
    M["sw_uncategorized"]= sw_uncategorized

    vendor_totals   = defaultdict(int)
    vendor_retailers= defaultdict(set)
    for d in sw_with_blocker:
        br = d.blocked_reason.lower()
        matched = False
        for vname, pats in VENDOR_PATTERNS.items():
            if any(p in br for p in pats):
                vendor_totals[vname]   += d.amount
                vendor_retailers[vname].add(d.name.split(" - ")[0].strip())
                matched = True
                break
        if not matched:
            vendor_totals["Other"] += d.amount
    M["vendor_totals"]   = dict(vendor_totals)
    M["vendor_retailers"]= {k: sorted(v) for k, v in vendor_retailers.items()}

    M["sw_top5"] = sorted(
        [d for d in sw_deals if not d.is_test_record()],
        key=lambda d: -d.amount
    )[:5]

    # --- Movement / new deals ---
    M["moved_this_week"] = [
        (d, ts) for d, ts in M["moved_this_week_active"]
    ]
    M["new_deals_this_week"] = [
        d for d in deals
        if d.created and d.created >= week_ago and not d.is_test_record()
    ]

    in_lab_new = [
        d for d in by_stage.get(in_lab_id, [])
        if d.entered_current and d.entered_current >= week_ago and not d.is_test_record()
    ]
    M["in_lab_new_this_week"] = sorted(in_lab_new, key=lambda d: -(d.amount or 0))

    # --- Lab median ---
    lab_days = [
        d.days_in_stage(asof) for d in by_stage.get(in_lab_id, [])
        if d.days_in_stage(asof) is not None and not d.is_test_record()
    ]
    M["lab_median_days"] = int(sorted(lab_days)[len(lab_days) // 2]) if lab_days else 0

    # --- Forward calendar ---
    in14 = asof + timedelta(days=14)
    fwd = [
        d for d in deals
        if d.next_activity and asof <= d.next_activity <= in14 and not d.is_test_record()
    ]
    M["fwd_calendar_top5"] = sorted(fwd, key=lambda d: d.next_activity)[:5]

    # --- Funnel breakdown ---
    def build_funnel_breakdown():
        FUNNEL_ORDER_KEYS = [
            "_leads", "_unqualified", "_qualified", "_get_started",
            "_setup_guide", "_legals", "onboarding_stage_id",
            "in_lab_stage_id", "awaiting_sw_stage_id",
            "awaiting_activation_stage_id", "awaiting_transactions_stage_id",
            "_active", "_closed_lost", "_parking_lot_other",
        ]
        STAGE_DISPLAY = {
            "in_lab_stage_id":               "In Lab",
            "awaiting_sw_stage_id":          "Await. SW",
            "awaiting_activation_stage_id":  "Await. Act.",
            "awaiting_transactions_stage_id":"Await. Trans",
            "onboarding_stage_id":           "Onboarding",
            "_active":                       "Active",
            "_qualified":                    "Qualified",
            "_get_started":                  "Get Started Form",
            "_setup_guide":                  "Setup Guide",
            "_legals":                       "Legals Signed",
            "_leads":                        "Leads",
            "_unqualified":                  "Unqualified",
            "_closed_lost":                  "Closed Lost",
            "_parking_lot_other":            "Parking Lot",
        }

        def role_key(stage_id):
            if stage_id in active_ids:
                return "_active"
            for k in ("in_lab_stage_id", "awaiting_sw_stage_id",
                      "awaiting_activation_stage_id",
                      "awaiting_transactions_stage_id", "onboarding_stage_id"):
                if payload.get(k) == stage_id:
                    return k
            label = (stage_labels.get(stage_id) or "").lower()
            if "qualified" in label and "unqualified" not in label:
                return "_qualified"
            if "get started" in label:
                return "_get_started"
            if "setup guide" in label:
                return "_setup_guide"
            if "legals" in label:
                return "_legals"
            if "unqualified" in label:
                return "_unqualified"
            if "leads" in label:
                return "_leads"
            if "closed lost" in label:
                return "_closed_lost"
            if "parking" in label:
                return "_parking_lot_other"
            return None

        rows = []
        used = set()
        for rk in FUNNEL_ORDER_KEYS:
            if rk == "_active":
                sids = list(active_ids)
            elif rk in ("in_lab_stage_id", "awaiting_sw_stage_id",
                        "awaiting_activation_stage_id",
                        "awaiting_transactions_stage_id", "onboarding_stage_id"):
                sid = payload.get(rk)
                sids = [sid] if sid else []
            else:
                sids = [sid for sid in stage_labels if role_key(sid) == rk]

            for sid in sids:
                if not sid or sid in used:
                    continue
                deals_here = by_stage.get(sid, [])
                if not deals_here:
                    continue
                used.add(sid)
                n = len(deals_here)
                stores = sum(d.amount for d in deals_here)
                dlist = [d.days_in_stage(asof) for d in deals_here if d.days_in_stage(asof) is not None]
                median = int(statistics.median(dlist)) if dlist else None
                rows.append({
                    "label": STAGE_DISPLAY.get(rk, stage_labels.get(sid, sid)),
                    "stage_id": sid,
                    "deals": n,
                    "stores": stores,
                    "median_days": median,
                })
        return rows

    M["funnel_breakdown"] = build_funnel_breakdown()

    # Stage velocity lookup: label -> median_days
    M["stage_velocity"] = {
        row["label"]: row["median_days"]
        for row in M["funnel_breakdown"]
        if row["median_days"] is not None
    }

    # --- Anomalies ---
    A = {}
    active_deal_list = [d for sid in active_ids for d in by_stage.get(sid, [])]
    A["test_in_active"]        = [d for d in active_deal_list if d.is_test_record()]
    A["test_in_active_stores"] = sum(d.amount for d in A["test_in_active"])
    A["active_legit_sum"]      = M["active_stores_deals"] - A["test_in_active_stores"]
    A["closedwon_no_amount"]   = [d for d in active_deal_list if d.amount_missing() and not d.is_test_record()]
    A["test_in_pipeline"]      = [d for d in deals
                                  if d.stage not in active_ids
                                  and d.stage not in {"closedlost"}
                                  and d.is_test_record()]
    mid_funnel = {in_lab_id, sw_id, act_id, trans_id, onb_id}
    A["pipeline_no_amount"]    = [d for d in deals
                                  if d.stage in mid_funnel
                                  and d.amount_missing()
                                  and not d.is_test_record()]
    A["zero_amount_active"]    = [d for d in deals
                                  if d.stage not in active_ids
                                  and d.stage not in {"closedlost"}
                                  and d.amount_zero_explicit()
                                  and not d.is_test_record()
                                  and d.stage not in EARLY_FUNNEL_STAGES]
    A["no_owner"]              = [d for d in deals
                                  if not d.owner
                                  and d.stage not in {"closedlost"}
                                  and d.stage not in EARLY_FUNNEL_STAGES
                                  and not d.is_test_record()]
    A["sw_no_reason"]          = [d for d in by_stage.get(sw_id, [])
                                  if not d.blocked_reason and not d.is_test_record()]
    A["sw_no_reason_stores"]   = sum(d.amount for d in A["sw_no_reason"])
    A["stale_early_funnel"]    = [d for d in deals
                                  if d.stage in EARLY_FUNNEL_STAGES
                                  and d.days_in_stage(asof) is not None
                                  and d.days_in_stage(asof) >= 90
                                  and not d.is_test_record()]

    # Duplicate names
    name_groups = defaultdict(list)
    for d in deals:
        if d.stage not in {"closedlost"} and not d.is_test_record():
            clean = d.name.split(" - New Deal")[0].split(" - New Date")[0].strip()
            if clean:
                name_groups[clean.lower()].append(d)
    A["dup_names"] = {k: v for k, v in name_groups.items() if len(v) > 1}

    M["anomalies"] = A

    # --- Funnel conversion ---
    committed_stores = sum(
        d.amount for d in deals
        if d.stage not in {"closedlost"}
        and d.stage not in EARLY_FUNNEL_STAGES
        and not d.is_test_record()
    )
    M["committed_stores"] = committed_stores
    M["funnel_conv_pct"]  = round(100 * M["active_stores"] / max(1, committed_stores))

    return M


# ============================================================
# STAGE DISPLAY HELPERS
# ============================================================
STAGE_DISPLAY_FULL = {
    "in_lab_stage_id":               "In Lab",
    "awaiting_sw_stage_id":          "Awaiting Software",
    "awaiting_activation_stage_id":  "Awaiting Activation",
    "awaiting_transactions_stage_id":"Awaiting Transactions",
    "onboarding_stage_id":           "Onboarding",
    "_active":                       "Active",
    "_qualified":                    "Qualified",
    "_get_started":                  "Get Started Form",
    "_setup_guide":                  "Setup Guide",
    "_legals":                       "Legals Signed",
    "_leads":                        "Leads",
    "_unqualified":                  "Unqualified",
    "_closed_lost":                  "Closed Lost",
    "_parking_lot_other":            "Parking Lot Other",
}


def stage_display(stage_id, payload):
    """Full canonical label for a stage."""
    active_ids = set(payload.get("active_stage_ids") or [])
    if stage_id in active_ids:
        return STAGE_DISPLAY_FULL["_active"]
    for k in ("in_lab_stage_id", "awaiting_sw_stage_id",
              "awaiting_activation_stage_id",
              "awaiting_transactions_stage_id", "onboarding_stage_id"):
        if payload.get(k) == stage_id:
            return STAGE_DISPLAY_FULL[k]
    label = (payload.get("stage_labels") or {}).get(stage_id, "") or ""
    norm = " ".join(label.lower().split())
    if "qualified" in norm and "unqualified" not in norm:
        return STAGE_DISPLAY_FULL["_qualified"]
    if "get started" in norm:
        return STAGE_DISPLAY_FULL["_get_started"]
    if "setup guide" in norm:
        return STAGE_DISPLAY_FULL["_setup_guide"]
    if "legals" in norm:
        return STAGE_DISPLAY_FULL["_legals"]
    if "unqualified" in norm:
        return STAGE_DISPLAY_FULL["_unqualified"]
    if "leads" in norm:
        return STAGE_DISPLAY_FULL["_leads"]
    if "closed lost" in norm:
        return STAGE_DISPLAY_FULL["_closed_lost"]
    if "parking" in norm:
        return STAGE_DISPLAY_FULL["_parking_lot_other"]
    return " ".join(str(label).split())


# ============================================================
# HTML BUILDER
# ============================================================
CSS = """
/* ── Reset & Root ─────────────────────────────────────── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --navy:       #1F3864;
  --navy-dark:  #16294A;
  --green:      #0F6E56;
  --amber:      #C58722;
  --red:        #A32635;
  --red-dark:   #8B2418;
  --ink:        #1a1a1a;
  --ink-soft:   #4a4a4a;
  --ink-faint:  #888888;
  --rule:       #cccccc;
  --rule-soft:  #e5e5e5;
  --bg-page:    #ffffff;
  --bg-kpi:     #FAFAF7;
  --bg-band:    #FEF4E8;
  --bg-stalled: #FBECEA;
  --bg-info:    #EEF2FB;
  --border-info:#3B58A8;
  --gray-200:   #e9edf1;
  --gray-300:   #d7dde3;
}

/* ── Outer shell ─────────────────────────────────────── */
html, body {
  background: #e6e8eb;
  font-family: 'Helvetica Neue', Arial, Helvetica, sans-serif;
  font-size: 13px;
  line-height: 1.3;
  color: var(--ink);
}
.report-page {
  width: 8.5in;
  min-height: 11in;
  margin: 20px auto;
  padding: 0.32in 0.4in 0.28in;
  background: var(--bg-page);
  box-shadow: 0 2px 10px rgba(0,0,0,0.12);
  page-break-after: always;
}
.report-page:last-child { page-break-after: auto; }

/* ── Page header ─────────────────────────────────────── */
.page-header { margin-bottom: 6px; }
.header-top { display: flex; justify-content: space-between; align-items: flex-start; }
.report-title { font-size: 26px; font-weight: 800; color: var(--navy); letter-spacing: 0.3px; }
.header-meta  { text-align: right; font-size: 11px; color: var(--ink-soft); line-height: 1.5; }
.header-rule  { height: 2px; background: var(--navy); margin: 5px 0 4px; }
.header-sub   { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--ink-faint); margin-bottom: 5px; }

/* ── Page 1 body ─────────────────────────────────────── */
.report-body { display: flex; flex-direction: column; gap: 5px; }

/* ── Callout bands ───────────────────────────────────── */
.bands { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.band {
  border-radius: 4px;
  padding: 7px 10px;
  border: 1px solid transparent;
}
.band-trajectory {
  background: var(--bg-band);
  border-color: #d4b88a;
}
.band-stalled {
  background: var(--bg-stalled);
  border-color: #d4a0a0;
}
.band-label {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-bottom: 2px;
}
.band-trajectory .band-label { color: var(--navy); }
.band-stalled   .band-label  { color: var(--red-dark); }
.band-headline { font-size: 18px; font-weight: 800; line-height: 1.1; }
.band-trajectory .band-headline { color: var(--navy); }
.band-stalled   .band-headline  { color: var(--red); }
.band-sub { font-size: 10.5px; margin-top: 2px; }
.band-trajectory .band-sub { color: var(--ink-soft); }
.band-stalled   .band-sub  { color: var(--ink-soft); }
.band-detail { font-size: 10px; color: var(--ink-soft); margin-top: 3px; }
.bucket-90p { color: var(--red-dark); font-weight: 700; }
.band-longest {
  margin-top: 4px;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--ink);
}
.band-longest span { font-weight: 400; color: var(--ink-soft); }

/* ── KPI row ─────────────────────────────────────────── */
.kpi-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 5px; }
.kpi-cell {
  background: var(--bg-kpi);
  border: 1px solid var(--rule-soft);
  border-radius: 4px;
  padding: 6px 8px 6px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
}
.kpi-label { font-size: 9px; font-weight: 700; text-transform: uppercase; color: var(--ink-faint); letter-spacing: 0.4px; margin-bottom: 2px; }
.kpi-value { font-size: 21px; font-weight: 800; line-height: 1; display: flex; align-items: baseline; gap: 3px; }
.kpi-arrow { font-size: 13px; font-weight: 700; }
.kpi-sub   { font-size: 9px; margin-top: 3px; color: var(--ink-soft); line-height: 1.3; }
.kpi-good  .kpi-value { color: var(--green); }
.kpi-good  .kpi-arrow { color: var(--green); }
.kpi-bad   .kpi-value { color: var(--red); }
.kpi-bad   .kpi-arrow { color: var(--red); }
.kpi-navy  .kpi-value { color: var(--navy); }
.kpi-black .kpi-value { color: var(--ink); }
.kpi-black .kpi-arrow { color: var(--ink); }

/* ── Two-col grid ────────────────────────────────────── */
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; }

/* ── Panel (bordered card) ───────────────────────────── */
.panel { border: 1px solid var(--gray-200); border-radius: 4px; overflow: hidden; }
.panel-header {
  background: var(--navy);
  color: #fff;
  font-size: 9.5px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  padding: 4px 8px;
}
.panel-header.red    { background: var(--red); }
.panel-header.amber  { background: var(--amber); }
.panel-header.meta   { font-size: 8.5px; font-weight: 400; float: right; text-transform: none; letter-spacing: 0; }
.panel-body { padding: 6px 8px; font-size: 11px; }

/* ── Action list ─────────────────────────────────────── */
.action-list { margin: 0; padding-left: 14px; font-size: 10.5px; color: var(--ink-soft); line-height: 1.7; }
.action-list li { margin-bottom: 1px; }
.action-list b  { color: var(--ink); }

/* ── Movement list ───────────────────────────────────── */
.move-list { list-style: none; padding: 0; font-size: 10.5px; color: var(--ink-soft); line-height: 1.7; }
.move-list li::before { margin-right: 5px; }
.move-active::before  { content: "●"; color: var(--green); }
.move-lab::before     { content: "↑"; color: var(--navy); }
.move-new::before     { content: "+"; color: var(--ink-faint); }

/* ── Tables ──────────────────────────────────────────── */
.data-table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
.data-table th {
  font-size: 9px;
  color: var(--ink-faint);
  text-transform: uppercase;
  letter-spacing: 0.3px;
  font-weight: 600;
  padding: 2px 5px 4px;
  border-bottom: 1px solid var(--rule);
  text-align: left;
}
.data-table td { padding: 3px 5px; border-bottom: 1px solid var(--rule-soft); color: var(--ink); }
.data-table tr:last-child td { border-bottom: none; }
.data-table .col-r { text-align: right; }
.data-table .red-flag { color: var(--red); font-weight: 700; }
.data-table .note-row td { font-size: 9.5px; color: var(--ink-faint); padding-top: 4px; border-bottom: none; }

/* ── Stage distribution bar chart ───────────────────── */
.stage-bars { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 18px; padding: 6px 8px 5px; font-size: 10px; }
.bar-row { display: flex; align-items: center; gap: 5px; margin-bottom: 2px; }
.bar-row .bar-label { width: 92px; text-align: right; color: var(--ink-soft); flex-shrink: 0; font-size: 10px; }
.bar-track { flex: 1; height: 7px; background: var(--gray-200); border-radius: 3px; overflow: hidden; }
.bar-fill  { height: 100%; background: var(--navy); border-radius: 3px; }
.bar-count { width: 22px; text-align: right; font-weight: 600; color: var(--ink); flex-shrink: 0; font-size: 10px; }

/* ── Page 2 ──────────────────────────────────────────── */
.page2-pills { display: grid; grid-template-columns: 1fr 1fr; gap: 5px; margin-bottom: 5px; }
.pill { border-radius: 4px; padding: 6px 10px; }
.pill-trajectory { background: var(--bg-band);    border: 1px solid #d4b88a; }
.pill-stalled    { background: var(--bg-stalled); border: 1px solid #d4a0a0; }
.pill-label { font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.4px; margin-bottom: 2px; }
.pill-trajectory .pill-label { color: var(--navy); }
.pill-stalled    .pill-label { color: var(--red-dark); }
.pill-main { font-size: 12.5px; font-weight: 700; }
.pill-trajectory .pill-main { color: var(--navy); }
.pill-stalled    .pill-main { color: var(--red); }
.pill-sub { font-size: 10px; color: var(--ink-soft); margin-top: 2px; }

/* ── Pipeline glance bar ─────────────────────────────── */
.pipeline-bar-wrap { padding: 6px 10px 8px; }
.pipeline-bar-numbers {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 5px;
  font-size: 11px;
}
.pipeline-bar-numbers .active-num {
  font-size: 22px;
  font-weight: 800;
  color: var(--green);
  line-height: 1;
}
.pipeline-bar-numbers .delta-num { font-size: 11px; color: var(--green); }
.pipeline-bar-numbers .meta-nums { font-size: 10px; color: var(--ink-faint); margin-left: 6px; }
.pipeline-bar {
  display: flex;
  height: 20px;
  border-radius: 3px;
  overflow: hidden;
  background: var(--gray-200);
  margin-bottom: 4px;
}
.bar-seg {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 8.5px;
  font-weight: 700;
  color: #fff;
  white-space: nowrap;
  padding: 0 4px;
  overflow: hidden;
}
.bar-axis {
  display: flex;
  justify-content: space-between;
  font-size: 8px;
  color: var(--ink-faint);
  padding: 0 1px;
}

/* ── Metrics table ───────────────────────────────────── */
.metric-table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
.metric-table th {
  font-size: 9px;
  color: #fff;
  background: var(--navy);
  padding: 4px 7px;
  text-align: left;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.metric-table td { padding: 4px 7px; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
.metric-table tr:last-child td { border-bottom: none; }
.metric-table .metric-name { font-weight: 600; color: var(--ink); white-space: nowrap; }
.metric-table .metric-value { font-weight: 700; color: var(--navy); white-space: nowrap; }

/* ── SW vendor bar ───────────────────────────────────── */
.sw-bar-wrap { margin: 4px 0 6px; }
.sw-bar { display: flex; height: 20px; border-radius: 3px; overflow: hidden; font-size: 9px; font-weight: 700; }
.sw-detail {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
  font-size: 10px;
  margin-top: 4px;
}
.sw-detail b { display: block; font-size: 11.5px; }
.sw-detail span { color: var(--ink-soft); }

/* ── Funnel two-col table ────────────────────────────── */
.funnel-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0; }
.funnel-table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
.funnel-table th {
  font-size: 9px;
  color: #fff;
  background: var(--navy);
  padding: 3px 6px;
  text-align: left;
}
.funnel-table td { padding: 3px 6px; border-bottom: 1px solid var(--rule-soft); }
.funnel-table tr:last-child td { border-bottom: none; }
.funnel-table .red-flag { color: var(--red); font-weight: 700; }

/* ── Page 3 ──────────────────────────────────────────── */
.intro-box {
  background: var(--bg-info);
  border-left: 3px solid var(--border-info);
  padding: 7px 10px;
  font-size: 11px;
  color: var(--ink-soft);
  border-radius: 0 4px 4px 0;
  margin-bottom: 6px;
}
.scorecard { width: 100%; border-collapse: collapse; font-size: 10.5px; margin-bottom: 6px; }
.scorecard th {
  font-size: 9px;
  color: #fff;
  background: var(--navy);
  padding: 4px 8px;
  text-align: left;
}
.scorecard td { padding: 4px 8px; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
.scorecard tr:last-child td { border-bottom: none; }
.priority-badge {
  display: inline-block;
  font-size: 8.5px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
.priority-critical { background: #f5c5be; color: #7d1c2c; }
.priority-high     { background: #fce9b7; color: #8a5d0a; }
.priority-medium   { background: #e1ecfb; color: #1a4a84; }
.priority-low      { background: var(--gray-200); color: var(--ink-faint); }
.anomaly-block { border: 1px solid var(--gray-200); border-radius: 4px; overflow: hidden; margin-bottom: 5px; }
.anomaly-header { background: var(--navy); color: #fff; font-size: 10px; font-weight: 700; padding: 4px 8px; text-transform: uppercase; letter-spacing: 0.3px; }
.anomaly-table { width: 100%; border-collapse: collapse; font-size: 10.5px; }
.anomaly-table th { font-size: 9px; color: #fff; background: #2a4a6e; padding: 3px 8px; text-align: left; }
.anomaly-table td { padding: 4px 8px; border-bottom: 1px solid var(--rule-soft); vertical-align: top; }
.anomaly-table tr:last-child td { border-bottom: none; }
.anomaly-table .test-flag { color: var(--red); font-weight: 700; }

/* ── Footer ──────────────────────────────────────────── */
.page-footer {
  display: flex;
  justify-content: space-between;
  font-size: 9.5px;
  color: var(--ink-faint);
  border-top: 1px solid var(--rule-soft);
  padding-top: 4px;
  margin-top: 6px;
}

/* ── Print ───────────────────────────────────────────── */
@media print {
  body { background: white; }
  .report-page { margin: 0; box-shadow: none; width: 100%; }
  .report-page:last-child { page-break-after: avoid; }
}
"""


def render_html(M, pulled_at_str, asof):
    payload   = M["payload"]
    A         = M["anomalies"]
    b         = M["stalled_buckets"]
    asof_label= M["asof_label"]
    pulled_dt = (parse_dt(M["pulled_at"]) or asof)
    pulled_fmt= fmt_date(pulled_dt, "%b %-d, %Y")

    # Pulled-at timestamp in military format: YYYY-MM-DD HH:MM ET
    if M["pulled_at"]:
        pd = parse_dt(M["pulled_at"])
        if pd:
            # Convert UTC to Eastern Time (UTC-4 EDT / UTC-5 EST)
            # Use fixed UTC-4 (EDT) as this covers most of the operational year
            from datetime import timedelta as _td
            et_offset = _td(hours=-4)
            pd_et = pd + et_offset
            pulled_ts = pd_et.strftime("%Y-%m-%d %H:%M ET")
        else:
            pulled_ts = pulled_at_str
    else:
        pulled_ts = pulled_at_str

    # ── Page 1 ──────────────────────────────────────────────────
    p1 = _page1(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A, b)
    p2 = _page2(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A)
    p3 = _page3(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>TruAge KPI &amp; Forecast Report — {asof_label}</title>
  <style>{CSS}</style>
</head>
<body>
{p1}
{p2}
{p3}
</body>
</html>"""


def _header(asof_label, pulled_ts, page_label, page_sub):
    return f"""
  <header class="page-header">
    <div class="header-top">
      <h1 class="report-title">TruAge KPI &amp; Forecast Report</h1>
      <div class="header-meta">
        <div>{h(asof_label)}</div>
        <div>Source: HubSpot live pull · {h(pulled_ts)}</div>
      </div>
    </div>
    <div class="header-rule"></div>
    <div class="header-sub">
      <span>{h(page_sub)}</span>
      <span>{h(page_label)}</span>
    </div>
  </header>"""


def _footer(left, right):
    return f"""
  <footer class="page-footer">
    <span>{h(left)}</span>
    <span>{h(right)}</span>
  </footer>"""


def _page1(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A, b):
    # Trajectory numbers
    active  = M["active_stores"]
    pace    = M["pace_per_month"]
    req     = M["required_pace"]
    mult    = M["pace_gap_multiple"]
    proj    = M["projected_total"]
    shortfall = M["shortfall"]
    months_left = M["months_left"]

    mult_str = f"{mult}×" if mult else "n/a"
    delta    = M["active_delta_week"]
    delta_str= f"+{delta:,} this week" if delta else "no change this week"

    # Stall bucket breakdown
    b3059   = b["30_59"]["stores"]
    b6089   = b["60_89"]["stores"]
    b90p    = b["90p"]["stores"]
    b30_stores = M["stalled_30d_stores"]
    b30_count  = M["stalled_30d_count"]

    # Longest stuck
    top_stalled = M["top5_stalled"][0] if M["top5_stalled"] else None
    if top_stalled:
        longest_html = (
            f'<div class="band-longest">'
            f'<a href="{h(deal_url(top_stalled["deals"][0].id))}" '
            f'data-id="{h(top_stalled["deals"][0].id)}">'
            f'{h(short_name(top_stalled["base"], 38))}'
            f'</a> ({top_stalled["stores"]:,} stores) — longest stuck: {h(top_stalled["days_str"])}d'
            f'<span> · {h(stage_display(top_stalled["stage"], payload))}</span>'
            f'</div>'
        )
    else:
        longest_html = ""

    # KPI row
    star_active = "*" if A["test_in_active"] else ""
    star_sw     = "*" if any(d.is_test_record() for d in M["by_stage"].get(M["sw_id"], [])) else ""
    kpi_ready   = M["ready_stores_real"]   if M["has_store_data"] else "—"
    kpi_pending = M["pending_stores_real"] if M["has_store_data"] else "—"
    fwd_count   = len(M["fwd_calendar_top5"])

    def _fmt_or_dash(value):
        """Thousands-separator format for numbers; pass placeholder strings
        (e.g. the "—" shown when has_store_data is False) through unchanged.
        Prevents ValueError: Cannot specify ',' with 's' when store-level
        data is temporarily unavailable from HubSpot."""
        return f"{value:,}" if isinstance(value, (int, float)) else str(value)

    # SW top 2 sub
    sw_top = M["sw_top5"][:2]
    sw_sub = " · ".join(f"{short_name(d.name,16)} {d.amount:,}" for d in sw_top) if sw_top else "see page 2"

    # What moved
    moved_lines = []
    for d, ts in M["moved_this_week_active"][:3]:
        date_str = fmt_date(ts, "%b %-d") if ts else ""
        stores_str = f"{d.amount:,} store{'s' if d.amount != 1 else ''}"
        moved_lines.append(
            f'<li class="move-active" data-id="{h(d.id)}">'
            f'<a href="{h(deal_url(d.id))}">{h(short_name(d.name, 34))}</a>'
            f' → Active (closed {h(date_str)}, {h(stores_str)})</li>'
        )
    for d in M["in_lab_new_this_week"][:2]:
        date_str = fmt_date(d.entered_current, "%b %-d") if d.entered_current else ""
        stores_str = f"{d.amount:,} stores" if d.amount else "store count TBD"
        moved_lines.append(
            f'<li class="move-lab" data-id="{h(d.id)}">'
            f'{h(short_name(d.name, 34))} → In Lab ({h(stores_str)}, entered {h(date_str)})</li>'
        )
    n_new = len(M["new_deals_this_week"])
    if n_new:
        names = ", ".join(short_name(d.name, 16) for d in M["new_deals_this_week"][:3])
        moved_lines.append(
            f'<li class="move-new"><b>{n_new} new deal{"s" if n_new != 1 else ""}</b>'
            f' entered pipeline this week ({h(names)})</li>'
        )
    if not moved_lines:
        moved_lines.append('<li style="color:var(--ink-faint)">No notable movement this week.</li>')

    # Required next week
    req_lines = []
    for g in M["top4_stalled_by_stores"]:
        reason = f", {h(g['reason'])}" if g["reason"] != "—" else ", no blocker logged"
        req_lines.append(
            f'<li><b>{h(short_name(g["base"], 32))}</b>'
            f' — {g["stores"]:,} stores, {h(g["days_str"])}d{reason}</li>'
        )
    rem_s = M["stalled_remaining_stores"]
    rem_d = M["stalled_remaining_deals"]
    if rem_s > 0:
        req_lines.append(
            f'<li><b>Remaining stalled</b> — {rem_s:,} stores across {rem_d} smaller deals (address opportunistically)</li>'
        )

    # Forward calendar table
    fwd_rows = ""
    for d in M["fwd_calendar_top5"]:
        date_str = fmt_date(d.next_activity, "%b %-d") if d.next_activity else "—"
        stores_str = f"{d.amount:,}" if d.amount else "—"
        fwd_rows += (
            f'<tr data-id="{h(d.id)}">'
            f'<td>{h(date_str)}</td>'
            f'<td><a href="{h(deal_url(d.id))}">{h(short_name(d.name, 26))}</a></td>'
            f'<td class="col-r">{h(stores_str)}</td></tr>'
        )
    if not fwd_rows:
        fwd_rows = '<tr><td colspan="3" style="color:var(--ink-faint)">No activities scheduled in next 14 days</td></tr>'

    # Awaiting SW table
    sw_rows = ""
    for d in M["sw_top5"]:
        blocker = d.blocked_reason or "Uncat."
        sw_rows += (
            f'<tr data-id="{h(d.id)}">'
            f'<td><a href="{h(deal_url(d.id))}">{h(short_name(d.name, 24))}</a></td>'
            f'<td>{h(blocker[:20])}</td>'
            f'<td class="col-r">{d.amount:,}</td></tr>'
        )

    # Stalled urgent rows
    def stall_rows(group_list, empty_msg):
        rows = ""
        for g in group_list:
            n = short_name(g["base"], 22)
            if g["is_group"]:
                n += f" · all {g['count']} rollouts"
            deal_id = g["deals"][0].id
            rows += (
                f'<tr data-id="{h(deal_id)}">'
                f'<td><a href="{h(deal_url(deal_id))}">{h(n)}</a></td>'
                f'<td>{h(stage_display(g["stage"], payload)[:14])}</td>'
                f'<td class="col-r">{g["stores"]:,}</td>'
                f'<td class="col-r">{h(g["days_str"])}</td></tr>'
            )
        if not rows:
            rows = f'<tr><td colspan="4" style="color:var(--ink-faint)">{h(empty_msg)}</td></tr>'
        return rows

    urgent_rows = stall_rows(M["top5_stalled_60p"],   "(none stalled 60+ days)")
    nudge_rows  = stall_rows(M["top5_stalled_30_59"], "(none in 30–59 day zone)")

    urgent_count  = len(M["stalled_60d_active"])
    urgent_stores = M["stalled_60d_stores"]
    nudge_count   = b["30_59"]["count"]
    nudge_stores  = b["30_59"]["stores"]

    # Pipeline distribution bars
    bars_html       = _pipeline_stacked_bar(M)
    scorecard_html  = _velocity_scorecard(M)

    # ── KPI cells with arrow/color logic ────────────────────────
    def kpi_cell(label, value, sub, star="", good_direction="up"):
        """
        good_direction: "up" = green when positive trend, red when negative
                        "down" = green when decreasing (Ready, Pending, Await SW)
                        "target" = special logic for calendar (green if >= 30)
        value is a number. Arrow and color derived from delta implied in sub.
        For directional coloring we use the week delta where available.
        """
        # Determine color class and arrow glyph from context
        # We'll pass pre-computed color_cls and arrow into the cell directly
        # so this helper is called with those already resolved
        raise NotImplementedError("use _kpi directly")

    def _kpi(label, num_str, sub, color_cls, arrow):
        return (
            f'<div class="kpi-cell {color_cls}">'
            f'<div class="kpi-label">{h(label)}</div>'
            f'<div class="kpi-value">{num_str}'
            f'<span class="kpi-arrow">{arrow}</span></div>'
            f'<div class="kpi-sub">{sub}</div>'
            f'</div>'
        )

    # Active Stores — up is good
    act_delta = M["active_delta_week"]
    if act_delta > 0:
        act_cls, act_arrow = "kpi-good", "↑"
    elif act_delta < 0:
        act_cls, act_arrow = "kpi-bad", "↓"
    else:
        act_cls, act_arrow = "kpi-navy", ""
    act_sub = f"+{act_delta:,} this week" if act_delta > 0 else ("no change this week" if act_delta == 0 else f"{act_delta:,} this week")

    # Ready — down is good (stores moving to Active)
    # We don't have a week delta for Ready/Pending/Total, so show neutral navy + no arrow
    # (future: compute weekly deltas for store status transitions)
    rdy_cls, rdy_arrow = "kpi-navy", ""

    # Pending — down is good
    pnd_cls, pnd_arrow = "kpi-navy", ""

    # Total Stores — up is good
    tot_cls, tot_arrow = "kpi-navy", ""

    # Awaiting Software — down is good
    # Use a simple heuristic: if sw_stores > 3000 flag amber/red; otherwise navy
    sw_val = M["sw_stores"]
    if sw_val > 3500:
        sw_cls, sw_arrow = "kpi-bad", "↑"
    else:
        sw_cls, sw_arrow = "kpi-navy", ""

    # Fwd Calendar — black; green if >= 30, red if < 30
    fwd_target = 30
    if fwd_count >= fwd_target:
        fwd_cls, fwd_arrow = "kpi-good", "↑"
    else:
        fwd_cls, fwd_arrow = "kpi-bad", "↓"

    kpi_cells_html = "".join([
        _kpi("Active Stores",     f"{active:,}{h(star_active)}", act_sub,          act_cls,  act_arrow),
        _kpi("Ready",             _fmt_or_dash(kpi_ready),       "onboarded · not transacting",          rdy_cls,  rdy_arrow),
        _kpi("Pending",           _fmt_or_dash(kpi_pending),     "contracts complete · not onboarded",   pnd_cls,  pnd_arrow),
        _kpi("Total Stores",      f"{M['stores_total_real']:,}", "all status buckets · ex-test",         tot_cls,  tot_arrow),
        _kpi("Awaiting Software", f"{M['sw_stores']:,}{h(star_sw)}", h(sw_sub),    sw_cls,   sw_arrow),
        _kpi("Fwd Calendar (14d)",str(fwd_count),                f"target {fwd_target}+",                fwd_cls,  fwd_arrow),
    ])

    return f"""
<section class="report-page p1">
  {_header(asof_label, pulled_ts, "Page 1 of 3", "Status")}
  <div class="report-body">

    <!-- Trajectory + Stalled bands -->
    <div class="bands">
      <div class="band band-trajectory">
        <div class="band-label">■ 2026 Trajectory Gap</div>
        <div class="band-headline">Tracking to {proj:,} · Goal {GOAL:,}</div>
        <div class="band-sub">{months_left} mo left. Pace: <b>{pace:,}/mo</b> Required: <b>{req:,}/mo ({mult_str})</b></div>
        <div class="band-detail">Shortfall: <b>{shortfall:,} stores</b></div>
      </div>
      <div class="band band-stalled">
        <div class="band-label">■ Stalled Commitments</div>
        <div class="band-headline">{b30_stores:,} stores stuck 30+ days</div>
        <div class="band-sub">30–59d: <b>{b3059:,}</b> · 60–89d: <b>{b6089:,}</b> · <span class="bucket-90p">90+d: {b90p:,}</span></div>
        {longest_html}
      </div>
    </div>

    <!-- KPI row -->
    <div class="kpi-row">{kpi_cells_html}</div>

    <!-- What Moved + Required Next Week -->
    <div class="grid-2">
      <div class="panel">
        <div class="panel-header">■ What Moved This Week</div>
        <div class="panel-body">
          <ul class="move-list">{''.join(moved_lines)}</ul>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header">■ Required Next Week to Bend the Curve</div>
        <div class="panel-body">
          <ol class="action-list">{''.join(req_lines)}</ol>
        </div>
      </div>
    </div>

    <!-- Forward Calendar — full width -->
    <div class="panel">
      <div class="panel-header">■ Forward Calendar — Next 5</div>
      <div class="panel-body" style="padding:4px 6px">
        <table class="data-table">
          <thead><tr><th>Date</th><th>Deal</th><th class="col-r">Stores</th></tr></thead>
          <tbody>{fwd_rows}</tbody>
        </table>
      </div>
    </div>

    <!-- Stalled boxes -->
    <div class="grid-2">
      <div class="panel">
        <div class="panel-header red">■ Stalled — Urgent (60+ Days)
          <span class="meta">{urgent_count} deals · {urgent_stores:,} stores</span>
        </div>
        <div class="panel-body" style="padding:4px 6px">
          <table class="data-table">
            <thead><tr><th>Deal</th><th>Stage</th><th class="col-r">Stores</th><th class="col-r">Days</th></tr></thead>
            <tbody>{urgent_rows}</tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <div class="panel-header amber">■ Stalled — Nudge Zone (30–59d)
          <span class="meta">{nudge_count} deals · {nudge_stores:,} stores</span>
        </div>
        <div class="panel-body" style="padding:4px 6px">
          <table class="data-table">
            <thead><tr><th>Deal</th><th>Stage</th><th class="col-r">Stores</th><th class="col-r">Days</th></tr></thead>
            <tbody>{nudge_rows}</tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- Committed Pipeline stacked bar -->
    <div class="panel">
      <div class="panel-header">■ Committed Pipeline — Stores by Stage</div>
      <div class="panel-body" style="padding:0">
        {bars_html}
      </div>
    </div>

    <!-- Velocity & Health scorecard -->
    <div class="panel">
      <div class="panel-header">■ Velocity &amp; Health — 4 Signals</div>
      <div class="panel-body" style="padding:6px 8px">
        {scorecard_html}
      </div>
    </div>

  </div>
  {_footer("TruAge KPI & Forecast Report · Page 1 of 3 · Status",
            "Trend charts, predictive signals & vendor exposure on reverse")}
</section>"""


def _velocity_scorecard(M):
    """
    Velocity & Health scorecard — 4 metrics in a 2x2 grid.
    Each metric has: label, primary value, interpretation bar or signal, verdict pill.
    """

    # ── 1. Activation Rate (pace acceleration) ──────────────────
    pace_cur   = M["pace_per_month"]
    pace_prior = M["pace_prior_month"]
    pace_delta = M["pace_acceleration"]
    req        = M["required_pace"]

    if pace_delta > 0:
        accel_signal = "good"
        accel_label  = f"+{pace_delta:,} vs prior 30d — accelerating"
    elif pace_delta < 0:
        accel_signal = "bad"
        accel_label  = f"{pace_delta:,} vs prior 30d — slowing"
    else:
        accel_signal = "neutral"
        accel_label  = "Flat vs prior 30d"

    pace_pct_of_req = min(100, round(100 * pace_cur / max(1, req)))
    # bar fill color
    pace_bar_color = "#0F6E56" if pace_pct_of_req >= 80 else ("#C58722" if pace_pct_of_req >= 40 else "#A32635")

    # ── 2. Stage Velocity — highlight slowest bottleneck ────────
    STAGE_BENCHMARKS = {
        "In Lab":        45,
        "Await. SW":     30,
        "Await. Act.":   21,
        "Await. Trans.": 14,
        "Onboarding":    30,
    }
    sv = M["stage_velocity"]
    velocity_rows = []
    for stage, benchmark in STAGE_BENCHMARKS.items():
        actual = sv.get(stage)
        if actual is None:
            continue
        over = actual - benchmark
        if over > 0:
            status = "bad"
            note   = f"+{over}d over benchmark"
        elif over >= -7:
            status = "neutral"
            note   = "within benchmark"
        else:
            status = "good"
            note   = f"{abs(over)}d under benchmark"
        velocity_rows.append((stage, actual, benchmark, status, note))
    # Sort worst first
    velocity_rows.sort(key=lambda x: -(x[1] - x[2]))

    def status_pill(status, text):
        colors = {"good": "#0F6E56", "bad": "#A32635", "neutral": "#C58722"}
        c = colors.get(status, "#888")
        return (f'<span style="background:{c};color:#fff;border-radius:3px;'
                f'padding:1px 5px;font-size:8px;font-weight:700;'
                f'white-space:nowrap">{h(text)}</span>')

    vel_html = ""
    for stage, actual, benchmark, status, note in velocity_rows[:4]:
        pill = status_pill(status, note)
        vel_html += (
            f'<div style="display:flex;align-items:center;justify-content:space-between;'
            f'padding:3px 0;border-bottom:1px solid #f0f0f0">'
            f'<span style="font-size:10px;color:#333;width:80px;flex-shrink:0">{h(stage)}</span>'
            f'<span style="font-size:13px;font-weight:800;color:#1F3864;width:28px;text-align:right">{actual}</span>'
            f'<span style="font-size:9px;color:#aaa;margin:0 4px">/{benchmark}d</span>'
            f'{pill}'
            f'</div>'
        )

    # ── 3. Funnel Conversion Rate ────────────────────────────────
    active    = M["active_stores"]
    committed = M["committed_stores"]
    conv_pct  = M["funnel_conv_pct"]
    # Benchmark: 20% is healthy for a sales pipeline at this stage
    conv_benchmark = 20
    if conv_pct >= conv_benchmark:
        conv_signal = "good"
        conv_note   = f"{conv_pct}% — at or above benchmark"
    elif conv_pct >= conv_benchmark // 2:
        conv_signal = "neutral"
        conv_note   = f"{conv_pct}% — below {conv_benchmark}% benchmark"
    else:
        conv_signal = "bad"
        conv_note   = f"{conv_pct}% — well below {conv_benchmark}% benchmark"
    conv_bar_pct   = min(100, round(conv_pct * 100 / max(1, conv_benchmark * 2)))
    conv_bar_color = "#0F6E56" if conv_signal == "good" else ("#C58722" if conv_signal == "neutral" else "#A32635")

    # ── 4. Concentration Risk ────────────────────────────────────
    conc_pct    = M["top3_concentration_pct"]
    conc_stores = M["top3_concentration_stores"]
    retailer_totals = M["retailer_store_totals"]
    top3_names = list(retailer_totals.keys())[:3]
    top3_vals  = list(retailer_totals.values())[:3]

    if conc_pct >= 60:
        conc_signal = "bad"
        conc_note   = f"High risk — top 3 = {conc_pct}% of pipeline"
    elif conc_pct >= 40:
        conc_signal = "neutral"
        conc_note   = f"Moderate — top 3 = {conc_pct}% of pipeline"
    else:
        conc_signal = "good"
        conc_note   = f"Healthy — top 3 = {conc_pct}% of pipeline"

    conc_rows = ""
    for name, stores in zip(top3_names, top3_vals):
        pct_of_pipeline = round(100 * stores / max(1, M["committed_stores"]))
        bar_w = min(100, pct_of_pipeline * 2)
        conc_rows += (
            f'<div style="display:flex;align-items:center;gap:5px;padding:2px 0">'
            f'<span style="font-size:9.5px;color:#333;width:110px;flex-shrink:0;'
            f'overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{h(name)}">'
            f'{h(name[:18])}{"…" if len(name)>18 else ""}</span>'
            f'<div style="flex:1;height:6px;background:#eee;border-radius:3px;overflow:hidden">'
            f'<div style="width:{bar_w}%;height:100%;background:#3A6BBF;border-radius:3px"></div></div>'
            f'<span style="font-size:9px;color:#555;width:38px;text-align:right">'
            f'{stores:,} ({pct_of_pipeline}%)</span>'
            f'</div>'
        )

    # ── Assemble 2×2 grid ────────────────────────────────────────
    cell_style = ("background:#FAFAF7;border:1px solid #e5e5e5;border-radius:4px;"
                  "padding:8px 10px;min-height:90px")
    label_style = ("font-size:8.5px;font-weight:700;text-transform:uppercase;"
                   "letter-spacing:0.5px;color:#888;margin-bottom:5px")

    accel_color = {"good": "#0F6E56", "bad": "#A32635", "neutral": "#C58722"}[accel_signal]

    cell1 = f"""
<div style="{cell_style}">
  <div style="{label_style}">Activation Rate — Last 30 Days</div>
  <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
    <span style="font-size:22px;font-weight:800;color:#1F3864">{pace_cur:,}</span>
    <span style="font-size:10px;color:#888">stores/mo &nbsp;·&nbsp; need {req:,}</span>
  </div>
  <div style="height:5px;background:#eee;border-radius:3px;overflow:hidden;margin-bottom:4px">
    <div style="width:{pace_pct_of_req}%;height:100%;background:{pace_bar_color};border-radius:3px"></div>
  </div>
  <div style="font-size:9px;color:{accel_color};font-weight:600">{h(accel_label)}</div>
  <div style="font-size:9px;color:#aaa;margin-top:1px">Prior 30d: {pace_prior:,} stores/mo</div>
</div>"""

    cell2 = f"""
<div style="{cell_style}">
  <div style="{label_style}">Stage Velocity — Median Days per Stage</div>
  {vel_html if vel_html else '<div style="font-size:10px;color:#aaa">No stage timing data</div>'}
</div>"""

    cell3 = f"""
<div style="{cell_style}">
  <div style="{label_style}">Funnel Conversion Rate</div>
  <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:4px">
    <span style="font-size:22px;font-weight:800;color:#1F3864">{conv_pct}%</span>
    <span style="font-size:10px;color:#888">{active:,} active of {committed:,} committed</span>
  </div>
  <div style="height:5px;background:#eee;border-radius:3px;overflow:hidden;margin-bottom:4px">
    <div style="width:{conv_bar_pct}%;height:100%;background:{conv_bar_color};border-radius:3px"></div>
  </div>
  <div style="font-size:9px;color:{conv_bar_color};font-weight:600">{h(conv_note)}</div>
</div>"""

    cell4 = f"""
<div style="{cell_style}">
  <div style="{label_style}">Concentration Risk — Top 3 Retailers</div>
  {conc_rows}
  <div style="margin-top:4px">{status_pill(conc_signal, conc_note)}</div>
</div>"""

    return f"""
<div style="display:grid;grid-template-columns:1fr 1fr;gap:5px;padding:0">
  {cell1}{cell2}{cell3}{cell4}
</div>"""


def _pipeline_stacked_bar(M):
    """
    Full-width stacked pipeline bar — one bar, segmented by stage,
    proportional to store count. Shows committed pipeline only
    (Onboarding through Active). Labels inside or below each segment.
    Color: dark navy (early) → medium navy → teal → green (Active).
    """
    # Segments in funnel order — store counts from M
    # We use deal-amount sums (which = store counts) for mid-funnel stages
    segments = [
        ("Onboarding",    M["onb_stores"],   "#1F3864"),  # dark navy
        ("In Lab",        M["in_lab_stores"], "#2C4D8A"),  # navy
        ("Await. SW",     M["sw_stores"],     "#3A6BBF"),  # mid blue
        ("Await. Act.",   M["act_stores"],    "#2980B9"),  # steel blue
        ("Await. Trans.", M["trans_stores"],  "#1A7A6E"),  # teal
        ("Active",        M["active_stores"], "#0F6E56"),  # green
    ]

    # Filter out zero-store segments
    segments = [(lbl, n, color) for lbl, n, color in segments if n > 0]
    total = sum(n for _, n, _ in segments)
    if total == 0:
        return '<div style="padding:10px;color:#888;font-size:11px">No pipeline data</div>'

    # Build SVG-style HTML bar — each segment as a flex child
    seg_html = ""
    label_html = ""
    for lbl, n, color in segments:
        pct = 100 * n / total
        # Only label segments wide enough to show text (>4%)
        show_label = pct > 4
        inner = (
            f'<span style="color:#fff;font-size:8.5px;font-weight:700;'
            f'white-space:nowrap;overflow:hidden;padding:0 4px;'
            f'text-overflow:ellipsis">{n:,}</span>'
            if show_label else ""
        )
        seg_html += (
            f'<div style="flex:{pct};background:{color};'
            f'display:flex;align-items:center;justify-content:center;'
            f'min-width:0;overflow:hidden" '
            f'title="{h(lbl)}: {n:,} stores ({pct:.1f}%)">'
            f'{inner}</div>'
        )
        # Label row below
        if show_label:
            label_html += (
                f'<div style="flex:{pct};text-align:center;'
                f'font-size:8px;color:#555;white-space:nowrap;'
                f'overflow:hidden;min-width:0;padding-top:3px">'
                f'{h(lbl)}</div>'
            )
        else:
            label_html += f'<div style="flex:{pct};min-width:0"></div>'

    return f"""
<div style="padding:8px 10px 6px">
  <div style="font-size:8.5px;font-weight:700;text-transform:uppercase;
              letter-spacing:0.5px;color:#888;margin-bottom:6px">
    Committed Pipeline — {total:,} stores across {len(segments)} active stages
  </div>
  <div style="display:flex;height:26px;border-radius:4px;overflow:hidden;
              border:1px solid #ddd">
    {seg_html}
  </div>
  <div style="display:flex;margin-top:2px">
    {label_html}
  </div>
</div>"""


def _stage_bars(M):
    """Horizontal bar chart for pipeline distribution (deal counts). Used on Page 2."""
    payload = M["payload"]

    FUNNEL_ORDER = [
        ("_leads", "Leads"),
        ("_qualified", "Qualified"),
        ("_get_started", "Get Started Form"),
        ("_setup_guide", "Setup Guide"),
        ("onboarding_stage_id", "Onboarding"),
        ("in_lab_stage_id", "In Lab"),
        ("awaiting_sw_stage_id", "Await. SW"),
        ("awaiting_activation_stage_id", "Await. Act."),
        ("awaiting_transactions_stage_id", "Await. Trans"),
        ("_active", "Active"),
        ("closedlost_direct", "Closed Lost"),
        ("_parking_lot_other", "Parking Lot"),
    ]

    active_ids = M["active_ids"]
    stage_labels = M["stage_labels"]

    def sid_for_key(rk):
        if rk == "_active":
            return list(active_ids)
        if rk == "closedlost_direct":
            return ["closedlost"]
        if rk in ("in_lab_stage_id", "awaiting_sw_stage_id",
                  "awaiting_activation_stage_id",
                  "awaiting_transactions_stage_id", "onboarding_stage_id"):
            sid = payload.get(rk)
            return [sid] if sid else []
        # fragment match
        frags = {
            "_leads": "leads", "_qualified": "qualified",
            "_get_started": "get started", "_setup_guide": "setup guide",
            "_legals": "legals", "_parking_lot_other": "parking",
        }
        frag = frags.get(rk)
        if frag:
            return [sid for sid, lbl in stage_labels.items()
                    if frag in lbl.lower()]
        return []

    rows = []
    for rk, lbl in FUNNEL_ORDER:
        sids = sid_for_key(rk)
        n = sum(len(M["by_stage"].get(s, [])) for s in sids)
        if n > 0:
            rows.append((lbl, n))

    if not rows:
        return '<div class="panel-body">No data</div>'

    max_n = max(n for _, n in rows)
    half = len(rows) // 2
    left_rows  = rows[:half]
    right_rows = rows[half:]

    def bar_row(lbl, n):
        pct = round(100 * n / max_n) if max_n else 0
        return (
            f'<div class="bar-row">'
            f'<span class="bar-label">{h(lbl)}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct}%"></div></div>'
            f'<span class="bar-count">{n}</span>'
            f'</div>'
        )

    left_html  = "".join(bar_row(l, n) for l, n in left_rows)
    right_html = "".join(bar_row(l, n) for l, n in right_rows)

    return f"""
  <div class="stage-bars">
    <div>{left_html}</div>
    <div>{right_html}</div>
  </div>"""


def _page2(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A):
    active  = M["active_stores"]
    pace    = M["pace_per_month"]
    req     = M["required_pace"]
    mult    = M["pace_gap_multiple"]
    proj    = M["projected_total"]
    months_left = M["months_left"]
    mult_str = f"{mult}×" if mult else "n/a"
    delta   = M["active_delta_week"]

    # Longest stuck pill
    top = M["top5_stalled"][0] if M["top5_stalled"] else None
    if top:
        longest_pill = (
            f'<div class="pill pill-stalled">'
            f'<div class="pill-label">■ Longest Stuck</div>'
            f'<div class="pill-main">'
            f'<a href="{h(deal_url(top["deals"][0].id))}">'
            f'{h(short_name(top["base"], 32))}</a>'
            f' ({top["stores"]:,} stores, {h(top["days_str"])}d)'
            f'</div>'
            f'<div class="pill-sub">{h(stage_display(top["stage"], payload))}</div>'
            f'</div>'
        )
    else:
        longest_pill = (
            '<div class="pill pill-stalled">'
            '<div class="pill-label">■ Longest Stuck</div>'
            '<div class="pill-main">No deals stalled 60+ days</div>'
            '</div>'
        )

    # Pipeline bar segments
    in_prog  = M["in_lab_stores"] + M["onb_stores"]
    sw_s     = M["sw_stores"]
    post_sw  = M["act_stores"] + M["trans_stores"]
    gap_seg  = max(0, GOAL - active - in_prog - sw_s - post_sw)
    total    = GOAL

    def seg_pct(v):
        return round(100 * v / total) if total else 0

    bar_segs = (
        f'<div class="bar-seg" style="width:{seg_pct(active)}%;background:var(--green)">'
        f'Active</div>'
        f'<div class="bar-seg" style="width:{seg_pct(in_prog)}%;background:#7eb074"></div>'
        f'<div class="bar-seg" style="width:{seg_pct(sw_s)}%;background:#e8a05a;min-width:60px">'
        f'Await. SW {sw_s:,}</div>'
        f'<div class="bar-seg" style="width:{seg_pct(post_sw)}%;background:#c9a23a"></div>'
        f'<div class="bar-seg" style="width:{seg_pct(gap_seg)}%;background:var(--gray-200)"></div>'
    )

    # Predictive signals table
    stalled_30_stores = M["stalled_30d_stores"]
    stalled_30_count  = M["stalled_30d_count"]
    fwd_count = len(M["fwd_calendar_top5"])
    lab_med   = M["lab_median_days"]
    sw_pct    = round(100 * M["sw_uncategorized"] / max(1, M["sw_stores"]))
    committed = M["committed_stores"]
    funnel_conv = M["funnel_conv_pct"]
    new_deals_n = len(M["new_deals_this_week"])

    signals_rows = f"""
    <tr>
      <td class="metric-name">1. Pace gap</td>
      <td class="metric-value">{pace:,}/mo vs {req:,}/mo</td>
      <td>{mult_str} gap vs Dec 31 target of {GOAL:,}. Most important number on this report. Target: close weekly.</td>
    </tr>
    <tr>
      <td class="metric-name">2. Stalled deals</td>
      <td class="metric-value">{stalled_30_count} deals · {stalled_30_stores:,} stores</td>
      <td>Deals not advancing 30+ days (excludes top-of-funnel). 30–59d nudge zone, 60+d urgent. Target: trending down.</td>
    </tr>
    <tr>
      <td class="metric-name">3. Fwd calendar</td>
      <td class="metric-value">{fwd_count} deals / next 14 days</td>
      <td>Healthy pipeline = 30+. Below target indicates unplanned or unlogged work. Target: 30+.</td>
    </tr>
    <tr>
      <td class="metric-name">4. Lab median</td>
      <td class="metric-value">~{lab_med} days</td>
      <td>Median days In Lab before advancing. Halving this doubles annual pace. Target: under 30 days.</td>
    </tr>
    <tr>
      <td class="metric-name">5. SW hygiene</td>
      <td class="metric-value">{sw_pct}% uncategorized</td>
      <td>{M["sw_uncategorized"]:,} stores Awaiting Software with no Blocked Reason. Operationally opaque. Target: under 20%.</td>
    </tr>
    <tr>
      <td class="metric-name">6. Active count</td>
      <td class="metric-value">{active:,} stores · {M["active_deal_count"]} deals</td>
      <td>Avg {(active / max(1, M["active_deal_count"])):.1f} stores/deal. Verify large outliers via Page 3 anomalies.</td>
    </tr>
    <tr>
      <td class="metric-name">7. Funnel conv.</td>
      <td class="metric-value">~{funnel_conv}% / {pace:,} per mo</td>
      <td>Of {committed:,} committed stores, % converted to Active. Doubling = doubling annual pace.</td>
    </tr>
    <tr>
      <td class="metric-name">8. New entries</td>
      <td class="metric-value">{new_deals_n} new deals this week</td>
      <td>Top-of-funnel velocity. Sustained low intake will starve the pipeline downstream.</td>
    </tr>"""

    # SW vendor bar
    sorted_vendors = sorted(M["vendor_totals"].items(), key=lambda kv: -kv[1])
    sw_total = M["sw_stores"]
    vendor_colors = ["var(--navy)", "#2f639d", "var(--red)", "#888888"]
    sw_bar_segs = ""
    for i, (vname, vstores) in enumerate(sorted_vendors[:4]):
        pct = round(100 * vstores / max(1, sw_total))
        color = vendor_colors[i % len(vendor_colors)]
        sw_bar_segs += (
            f'<div class="bar-seg" style="width:{pct}%;background:{color};min-width:30px">'
            f'{h(vname.split("(")[0].strip())} {vstores:,} ({pct}%)</div>'
        )
    unc_pct = round(100 * M["sw_uncategorized"] / max(1, sw_total))
    if unc_pct > 0:
        sw_bar_segs += (
            f'<div class="bar-seg" style="width:{unc_pct}%;background:var(--gray-300);'
            f'color:var(--ink-soft)">Uncat. {unc_pct}%</div>'
        )

    sw_detail_html = ""
    for vname, vstores in sorted_vendors[:3]:
        retailers = M["vendor_retailers"].get(vname, [])
        ret_str = " · ".join(short_name(r, 22) for r in sorted(retailers, key=lambda r: -sum(
            d.amount for d in M["by_stage"].get(M["sw_id"], [])
            if d.blocked_reason and any(p in d.blocked_reason.lower() for p in VENDOR_PATTERNS.get(vname, []))
            and short_name(d.name, 40).startswith(r[:8])
        ))[:3]) or "—"
        sw_detail_html += (
            f'<div class="sw-detail-item">'
            f'<b>{h(vname)}</b>'
            f'<span>{vstores:,} stores · {round(100*vstores/max(1,sw_total))}%</span><br>'
            f'<span>{h(ret_str)}</span>'
            f'</div>'
        )
    if M["sw_uncategorized"] > 0:
        sw_detail_html += (
            f'<div class="sw-detail-item">'
            f'<b>Uncategorized</b>'
            f'<span>{M["sw_uncategorized"]:,} stores · {unc_pct}%</span><br>'
            f'<span>No Blocked Reason set — see Signal 5</span>'
            f'</div>'
        )

    # Funnel table
    rows = M["funnel_breakdown"]
    half = (len(rows) + 1) // 2

    def funnel_row(r):
        med = r["median_days"]
        threshold = 60 if r["label"] in ("Await. SW", "Await. Act.", "Await. Trans", "Onboarding") else 90
        flag = med is not None and med > threshold
        med_str = f"~{med}" if med is not None else "—"
        med_cls = ' class="red-flag"' if flag else ""
        return (
            f'<tr><td>{h(r["label"])}</td>'
            f'<td class="col-r">{r["deals"]}</td>'
            f'<td class="col-r">{r["stores"]:,}</td>'
            f'<td class="col-r"{med_cls}>{h(med_str)}</td></tr>'
        )

    left_funnel  = "".join(funnel_row(r) for r in rows[:half])
    right_funnel = "".join(funnel_row(r) for r in rows[half:])

    return f"""
<section class="report-page p2">
  {_header(asof_label, pulled_ts, "Page 2 of 3", "Deep Dive — Trend, Signals & Vendor Exposure")}
  <div class="report-body">

    <!-- Summary pills -->
    <div class="page2-pills">
      <div class="pill pill-trajectory">
        <div class="pill-label">■ 2026 Trajectory</div>
        <div class="pill-main">Tracking to {proj:,} · Goal {GOAL:,}</div>
        <div class="pill-sub">{mult_str} pace gap ({pace:,}/mo vs {req:,}/mo required)</div>
      </div>
      {longest_pill}
    </div>

    <!-- Pipeline at a glance -->
    <div class="panel">
      <div class="panel-header">■ Pipeline at a Glance — Path to {GOAL:,} by {GOAL_DATE_STR}</div>
      <div class="pipeline-bar-wrap">
        <div class="pipeline-bar-numbers">
          <span class="active-num">{active:,}</span>
          <span class="delta-num">+{delta} this week</span>
          <span class="meta-nums">
            In Progress {M["in_lab_stores"]:,} &nbsp;|&nbsp;
            Await. Act/Trans {(M["act_stores"]+M["trans_stores"]):,} &nbsp;|&nbsp;
            Pipeline gap {(GOAL - active - M["in_progress_stores"]):,}
          </span>
        </div>
        <div class="pipeline-bar">{bar_segs}</div>
        <div class="bar-axis">
          <span>0</span><span>5K</span><span>10K</span>
          <span>15K</span><span>20K</span><span>25K ▸</span>
        </div>
      </div>
    </div>

    <!-- Predictive signals -->
    <div class="panel">
      <div class="panel-header">■ Predictive Signals — 8 Metrics Tracking Whether Trajectory Is Bending</div>
      <table class="metric-table">
        <thead>
          <tr><th>Metric</th><th>Value</th><th>Interpretation</th></tr>
        </thead>
        <tbody>{signals_rows}</tbody>
      </table>
    </div>

    <!-- SW vendor exposure -->
    <div class="panel">
      <div class="panel-header">■ Software Dependency Exposure — {M["sw_categorized"]:,} Categorized of {sw_total:,} Awaiting Software
        <span style="font-size:8.5px;font-weight:400;float:right">{M["sw_uncategorized"]:,} stores uncategorized — no Blocked Reason set (see Signal 5)</span>
      </div>
      <div class="panel-body">
        <div class="sw-bar-wrap">
          <div class="sw-bar">{sw_bar_segs}</div>
        </div>
        <div class="sw-detail">{sw_detail_html}</div>
      </div>
    </div>

    <!-- Stage-by-stage funnel -->
    <div class="panel">
      <div class="panel-header">■ Stage-by-Stage Funnel — All Stages with Deals
        <span style="font-size:8.5px;font-weight:400;float:right">Median days flagged red where unusually high for the stage</span>
      </div>
      <div style="padding:0">
        <div class="funnel-grid">
          <table class="funnel-table">
            <thead><tr><th>Stage</th><th class="col-r">Deals</th><th class="col-r">Stores</th><th class="col-r">Days</th></tr></thead>
            <tbody>{left_funnel}</tbody>
          </table>
          <table class="funnel-table">
            <thead><tr><th>Stage</th><th class="col-r">Deals</th><th class="col-r">Stores</th><th class="col-r">Days</th></tr></thead>
            <tbody>{right_funnel}</tbody>
          </table>
        </div>
      </div>
    </div>

  </div>
  {_footer("TruAge KPI & Forecast Report · Page 2 of 3 · Deep Dive", f"Source: HubSpot live pull, {pulled_fmt}")}
</section>"""


def _page3(M, asof, asof_label, pulled_ts, pulled_fmt, payload, A):
    active = M["active_stores"]

    # Priority scorecard rows
    score_rows = []

    def add_row(anomaly, detail, priority):
        cls = {"CRITICAL": "critical", "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}[priority]
        score_rows.append(
            f'<tr><td>{h(anomaly)}</td><td>{h(detail)}</td>'
            f'<td><span class="priority-badge priority-{cls}">{priority}</span></td></tr>'
        )

    # Active count mismatch
    cw_sum = sum(d.amount for sid in M["active_ids"] for d in M["by_stage"].get(sid, []))
    cw_legit = A["active_legit_sum"]
    gap = active - cw_legit
    if M["has_store_data"]:
        add_row(
            "Active count: Stores ↔ Deal Amount mismatch",
            f"Stores object: {active:,} active · Deal Amount sum (legit): {cw_legit:,} · Gap: +{gap:,} stores",
            "CRITICAL"
        )

    if A["test_in_active"]:
        test_stores = A["test_in_active_stores"]
        add_row(
            "Test record(s) in Closed Won",
            f"{len(A['test_in_active'])} record · {test_stores:,} stores · primary inflation source",
            "CRITICAL"
        )

    if A["test_in_pipeline"]:
        add_row(
            "Test/junk deal names in live pipeline",
            f"{len(A['test_in_pipeline'])} records inflate stage counts",
            "HIGH"
        )

    if A["pipeline_no_amount"]:
        add_row(
            "Pipeline deals missing Amount/store count",
            f"{len(A['pipeline_no_amount'])} deals (mid/late funnel) — unknown store impact",
            "HIGH"
        )

    if A["closedwon_no_amount"]:
        names = ", ".join(short_name(d.name, 20) for d in A["closedwon_no_amount"][:3])
        add_row(
            "Closed Won deals with no Amount field",
            f"{len(A['closedwon_no_amount'])} deals: {names}",
            "HIGH"
        )

    if A["zero_amount_active"]:
        names = ", ".join(short_name(d.name, 18) for d in A["zero_amount_active"][:3])
        add_row(
            "Deals set to $0 in active pipeline",
            f"{len(A['zero_amount_active'])} deals: {names}",
            "MEDIUM"
        )

    if A["no_owner"]:
        names = ", ".join(short_name(d.name, 18) for d in A["no_owner"][:3])
        add_row(
            "Deals with no owner assigned",
            f"{len(A['no_owner'])} deals: {names}",
            "MEDIUM"
        )

    if A["sw_no_reason"]:
        add_row(
            "Awaiting Software deals with no Blocked Reason",
            f"{len(A['sw_no_reason'])} deals · {A['sw_no_reason_stores']:,} stores — opaque pipeline band",
            "MEDIUM"
        )

    if A["stale_early_funnel"]:
        add_row(
            "Stale early-funnel prospects (90+ days)",
            f"{len(A['stale_early_funnel'])} deals in Get Started/Qualified/Setup Guide stages",
            "MEDIUM"
        )

    if A["dup_names"]:
        sample = ", ".join(list(A["dup_names"].keys())[:3])
        add_row(
            "Duplicate deal names in pipeline",
            f"{len(A['dup_names'])} duplicate-name groups: {sample}…",
            "LOW"
        )

    scorecard_body = "".join(score_rows)

    # Anomaly 1 — active count detail
    a1_rows = ""
    if M["has_store_data"]:
        a1_rows += f"""
    <tr>
      <td>Stores ↔ Deals reconciliation</td>
      <td>HubSpot Stores: <b>{active:,}</b> active. Closed-won Amount sum (excl. test): <b>{cw_legit:,}</b>. Gap: <b>+{gap:,}</b>.</td>
      <td>Report headline {active:,} matches Stores. But ~{gap:,} stores activated without deal Amount being updated.</td>
      <td>Update Amount on closed-won deals so deal-pipeline metrics match the Stores object.</td>
    </tr>"""

    for d in A["test_in_active"]:
        a1_rows += f"""
    <tr>
      <td class="test-flag"><a href="{h(deal_url(d.id))}" data-id="{h(d.id)}">{h(short_name(d.name, 28))}</a> in Closed Won</td>
      <td>Test record closed Won with Amount = {d.amount:,}. Primary driver of inflation.</td>
      <td>Inflates active count by {d.amount:,}. Should not exist in production.</td>
      <td>Delete or archive. Do not count toward active stores.</td>
    </tr>"""

    for d in A["closedwon_no_amount"]:
        a1_rows += f"""
    <tr>
      <td>Closedwon with no Amount field</td>
      <td><a href="{h(deal_url(d.id))}" data-id="{h(d.id)}"><b>{h(short_name(d.name, 28))}</b></a></td>
      <td>Stores completely uncounted in all reporting.</td>
      <td>Set Amount. Confirm store count with deal owner.</td>
    </tr>"""

    # Anomaly 2 — pipeline no amount (top 14)
    mid_funnel = {M["in_lab_id"], M["sw_id"], M["act_id"], M["trans_id"], M["onb_id"]}
    stage_short_map = {
        M["in_lab_id"]: "In Lab", M["sw_id"]: "Await. SW",
        M["act_id"]: "Await. Act.", M["trans_id"]: "Await. Trans",
        M["onb_id"]: "Onboarding",
    }
    top14 = sorted(A["pipeline_no_amount"], key=lambda d: d.created or datetime.min, reverse=True)[:14]
    a2_rows = ""
    for d in top14:
        is_test = d.is_test_record()
        stage_lbl = stage_short_map.get(d.stage, d.stage)
        created_str = fmt_date(d.created, "%b %-d") if d.created else "—"
        issue = "Test record in live pipeline — remove" if is_test else "No amount"
        row_cls = ' class="test-flag"' if is_test else ""
        a2_rows += (
            f'<tr data-id="{h(d.id)}">'
            f'<td{row_cls}><a href="{h(deal_url(d.id))}">{h(short_name(d.name, 28))}</a></td>'
            f'<td>{h(stage_lbl)}</td>'
            f'<td>{h(created_str)}</td>'
            f'<td{row_cls}>{h(issue)}</td></tr>'
        )

    no_amt_total = len(A["pipeline_no_amount"])

    return f"""
<section class="report-page p3">
  {_header(asof_label, pulled_ts, "Page 3 of 3", "Data Quality & Action Items")}
  <div class="report-body">

    <div class="intro-box">
      This page lists data anomalies detected in the {h(fmt_date(asof, "%B %-d, %Y"))} HubSpot pull.
      Items are prioritized by impact on report accuracy. Each row identifies the records,
      the issue, and the action — fixing them before the next pull lets the report run
      cleanly without manual reconciliation.
    </div>

    <!-- Priority scorecard -->
    <div class="anomaly-block">
      <div class="anomaly-header">Priority Scorecard — All Anomalies This Week</div>
      <table class="scorecard">
        <thead><tr><th>Anomaly</th><th>Detail</th><th>Priority</th></tr></thead>
        <tbody>{scorecard_body}</tbody>
      </table>
    </div>

    <!-- Anomaly 1 -->
    <div class="anomaly-block">
      <div class="anomaly-header">Anomaly 1 — Active Count: Stores vs. Deal Amounts</div>
      <table class="anomaly-table">
        <thead><tr><th>Issue</th><th>Detail</th><th>Impact</th><th>Action</th></tr></thead>
        <tbody>{a1_rows}</tbody>
      </table>
    </div>

    <!-- Anomaly 2 -->
    <div class="anomaly-block">
      <div class="anomaly-header">Anomaly 2 — Pipeline Deals Missing Store Count ({no_amt_total} Total — Top 14 Shown)</div>
      <table class="anomaly-table">
        <thead><tr><th>Deal</th><th>Stage</th><th>Created</th><th>Issue</th></tr></thead>
        <tbody>{a2_rows}</tbody>
      </table>
    </div>

  </div>
  {_footer("TruAge KPI & Forecast Report · Page 3 of 3 · Data Anomalies",
            "For Data Team — action before next weekly pull")}
</section>"""


# ============================================================
# MAIN
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Generate TruAge HTML report")
    parser.add_argument("--date",   default=None, help="Report date YYYY-MM-DD (default: today)")
    parser.add_argument("--input",  default="hubspot_pull.json", help="Input JSON file")
    parser.add_argument("--output", default=None, help="Output HTML file (default: TruAge_Activation_Report_<date>.html)")
    args = parser.parse_args()

    # Date
    if args.date:
        asof = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        asof = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Output path
    date_str = asof.strftime("%Y-%m-%d")
    out_path = args.output or f"TruAge_KPI_Forecast_Report_{date_str}.html"

    print(f"Reading {args.input}…")
    payload, deals = load_data(args.input)

    print(f"Computing metrics for week ending {date_str}…")
    M = compute_metrics(payload, deals, asof)

    # Summary print
    print(f"  Active: {M['active_stores']:,} ({M['active_deal_count']} deals)")
    if M["has_store_data"]:
        print(f"  Ready: {M['ready_stores_real']:,}  Pending: {M['pending_stores_real']:,}")
    print(f"  In Lab: {M['in_lab_stores']:,} | Await SW: {M['sw_stores']:,} | Await Act: {M['act_stores']:,} | Await Trans: {M['trans_stores']:,}")
    print(f"  Stalled 30+ (active funnel): {M['stalled_30d_count']} deals, {M['stalled_30d_stores']:,} stores")
    print(f"  Pace: {M['pace_per_month']:,}/mo · Required: {M['required_pace']:,}/mo")
    a = M["anomalies"]
    print(
        f"  Anomalies: test-active={len(a['test_in_active'])}, "
        f"cw-no-amt={len(a['closedwon_no_amount'])}, "
        f"pipe-no-amt={len(a['pipeline_no_amount'])}, "
        f"test-pipe={len(a['test_in_pipeline'])}, "
        f"no-owner={len(a['no_owner'])}, "
        f"sw-no-reason={len(a['sw_no_reason'])}, "
        f"dups={len(a['dup_names'])}, "
        f"stale-early={len(a['stale_early_funnel'])}"
    )

    pulled_at_str = (parse_dt(payload.get("pulled_at")) or asof).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Rendering HTML…")
    html = render_html(M, pulled_at_str, asof)

    Path(out_path).write_text(html, encoding="utf-8")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
