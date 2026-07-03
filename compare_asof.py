#!/usr/bin/env python3
"""
compare_asof.py — Compare TruAge KPI metrics across multiple as-of dates
using a SINGLE HubSpot data pull.

USAGE:
    python compare_asof.py --input hubspot_pull.json --dates 2026-06-29 2026-06-30 2026-07-01

Reuses generate_report_html.py's load_data()/compute_metrics() so the
numbers here are computed with the exact same logic as the live report —
this is not a reimplementation, it just calls the same functions with
different asof values.

CAVEAT — read before trusting the output:
This reconstructs "as of <date>" metrics from ONE fresh pull's deal data
(stage-entry timestamps, createdate, closedate, etc.). That's accurate for
any deal that still exists in HubSpot today in roughly its historical
shape. It will NOT reflect deals that were deleted, merged, or heavily
re-edited since the earlier date — there is no stored historical snapshot
for 6/29 or 6/30 anywhere in this repo or in production (production only
keeps the latest pull, in /tmp, which resets on every redeploy/refresh).
Treat this as "what the report computes today, evaluated as of each date"
rather than a true historical replay.
"""
import argparse
from datetime import datetime, timezone

import generate_report_html as g


FIELDS = [
    ("active_stores",    "Active stores"),
    ("committed_stores",  "Committed pipeline (funnel calc, test-filtered)"),
    ("funnel_conv_pct",   "Funnel conversion %"),
    ("in_lab_stores",     "In Lab"),
    ("sw_stores",         "Awaiting SW"),
    ("act_stores",        "Awaiting Activation"),
    ("trans_stores",      "Awaiting Transactions"),
    ("onb_stores",        "Onboarding"),
]


def main():
    parser = argparse.ArgumentParser(description="Compare TruAge KPIs across as-of dates from one pull")
    parser.add_argument("--input", default="hubspot_pull.json")
    parser.add_argument("--dates", nargs="+", required=True, help="YYYY-MM-DD list, e.g. 2026-06-29 2026-06-30 2026-07-01")
    args = parser.parse_args()

    payload, deals = g.load_data(args.input)

    rows = []
    for d in args.dates:
        asof = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        M = g.compute_metrics(payload, deals, asof)
        M["_bar_total"] = (
            M.get("onb_stores", 0) + M.get("in_lab_stores", 0) + M.get("sw_stores", 0)
            + M.get("act_stores", 0) + M.get("trans_stores", 0) + M.get("active_stores", 0)
        )
        rows.append((d, M))

    header = f"{'Metric':46}" + "".join(f"{d:>14}" for d, _ in rows)
    print(header)
    print("-" * len(header))
    for key, label in FIELDS:
        line = f"{label:46}"
        for _, M in rows:
            val = M.get(key, "—")
            line += f"{val:>14,}" if isinstance(val, (int, float)) else f"{str(val):>14}"
        print(line)

    line = f"{'Pipeline bar total (unfiltered stage_sum)':46}"
    for _, M in rows:
        line += f"{M['_bar_total']:>14,}"
    print(line)

    line = f"{'  ^ gap vs. committed_stores (test records?)':46}"
    for _, M in rows:
        line += f"{M['_bar_total'] - M.get('committed_stores', 0):>14,}"
    print(line)

    print()
    print("Day-over-day deltas:")
    for i in range(1, len(rows)):
        d0, M0 = rows[i - 1]
        d1, M1 = rows[i]
        print(f"  {d0} -> {d1}:")
        for key, label in FIELDS:
            v0, v1 = M0.get(key), M1.get(key)
            if isinstance(v0, (int, float)) and isinstance(v1, (int, float)):
                delta = v1 - v0
                sign = "+" if delta >= 0 else ""
                print(f"    {label:46} {sign}{delta:,}")


if __name__ == "__main__":
    main()
