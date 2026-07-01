#!/usr/bin/env python3
"""
TruAge Activation Period Report
===============================

Additive reporting script for a fixed reporting window. This does not modify or
replace the existing weekly TruAge Activation report.

Outputs:
  - HTML summary report
  - activated stores CSV
  - closed-won deals CSV
  - owner summary CSV
  - raw HubSpot pull JSON

Default period:
  2026-01-01 through 2026-05-20

Usage:
  export HUBSPOT_TOKEN="pat-na1-..."
  python generate_period_report.py
  python generate_period_report.py --start-date 2026-01-01 --end-date 2026-05-20
  python generate_period_report.py --output-dir /mnt/c/Users/paulz/Downloads/truage-period-report
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import requests

from fetch_from_hubspot import (
    PIPELINE_ID,
    STORE_OBJECT_TYPE,
    STAGE_ROLES,
    fetch_all_deals,
    fetch_all_stores,
    fetch_stage_labels,
    summarize_stores,
)


TEST_SUBSTRING_PATTERNS = [
    "thinksys", "qrjwxjqsbuxciwmljofcd", "demo unit",
    "homeless not helpless", "muhammad hassan", "mendietaaaa", "bunny palace",
]
TEST_EXACT_NAMES = {
    "tester", "self employed", "send proud", "rita", "pan", "na", "clover",
}
EXCLUDE_TOKENS = (
    "test",
    "thinksys",
    "lab",
    "demo",
    "sandbox",
    "pilot test",
    "store test",
    "truage store test",
    "mesa-test",
)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_int(value) -> int:
    if value in (None, "", "null"):
        return 0
    try:
        return int(float(value))
    except Exception:
        return 0


def fmt_date(dt: datetime, pattern: str = "%b %-d, %Y") -> str:
    pat = pattern.replace("%-d", "%d").replace("%-m", "%m")
    rendered = dt.strftime(pat)
    rendered = re.sub(r" 0(\d)", r" \1", rendered)
    rendered = re.sub(r"^0(\d)", r"\1", rendered)
    return rendered


def slug_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def default_output_dir(start_date: datetime, end_date: datetime) -> Path:
    downloads = Path("/mnt/c/Users/paulz/Downloads")
    dirname = f"truage-period-report_{slug_date(start_date)}_to_{slug_date(end_date)}"
    return downloads / dirname if downloads.exists() else Path.cwd() / dirname


def is_test_deal_name(name: str) -> bool:
    normalized = (name or "").strip().lower()
    if not normalized:
        return False
    compact = normalized.split(" - new deal")[0].strip()
    if compact in TEST_EXACT_NAMES:
        return True
    return any(pattern in normalized for pattern in TEST_SUBSTRING_PATTERNS)


def is_test_store(store: dict) -> bool:
    return (store.get("is_test_data") or "").lower() == "true"


def contains_excluded_token(*values: str | None) -> bool:
    haystack = " ".join((value or "").strip().lower() for value in values if value)
    return any(token in haystack for token in EXCLUDE_TOKENS)


def normalize_status(store: dict) -> str:
    status = (store.get("status") or "").strip()
    if not status:
        return "(No Status)"
    return status.title()


def owner_key(value: str | None) -> str:
    value = (value or "").strip()
    return value or "(Unassigned)"


def fetch_owner_map(token: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": 500, "archived": "false"}
    after = None
    owner_map: dict[str, str] = {}

    while True:
        if after:
            params["after"] = after
        else:
            params.pop("after", None)
        resp = requests.get("https://api.hubapi.com/crm/v3/owners", headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"WARNING: owner lookup failed with {resp.status_code}; using owner IDs where names are unavailable.", file=sys.stderr)
            print(resp.text, file=sys.stderr)
            return owner_map
        data = resp.json()
        for owner in data.get("results", []):
            owner_id = str(owner.get("id") or "").strip()
            first = (owner.get("firstName") or "").strip()
            last = (owner.get("lastName") or "").strip()
            full_name = f"{first} {last}".strip() or (owner.get("email") or "").strip() or owner_id
            if owner_id:
                owner_map[owner_id] = full_name
            user_id = str(owner.get("userId") or "").strip()
            if user_id:
                owner_map[user_id] = full_name
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break

    return owner_map


def owner_name(value: str | None, owner_map: dict[str, str]) -> str:
    key = owner_key(value)
    return owner_map.get(key, key)


def deal_closedwon_date(deal: dict) -> datetime | None:
    return parse_dt(deal.get("hs_v2_date_entered_closedwon")) or parse_dt(deal.get("closedate"))


def store_activated_date(store: dict) -> datetime | None:
    return parse_dt(store.get("activated_at"))


def in_period(ts: datetime | None, start_date: datetime, end_date: datetime) -> bool:
    return bool(ts and start_date <= ts <= end_date)


def month_bucket(ts: datetime) -> str:
    return ts.strftime("%Y-%m")


def month_bucket_label(bucket: str, start_date: datetime, end_date: datetime) -> str:
    dt = datetime.strptime(bucket + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    label = dt.strftime("%b %Y")
    if dt.year == end_date.year and dt.month == end_date.month and end_date.day != calendar_days_in_month(dt):
        return f"{label} (through {end_date.day})"
    return label


def calendar_days_in_month(dt: datetime) -> int:
    next_month = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (next_month - timedelta(days=1)).day


def build_month_sequence(start_date: datetime, end_date: datetime) -> list[str]:
    seq = []
    cursor = start_date.replace(day=1)
    limit = end_date.replace(day=1)
    while cursor <= limit:
        seq.append(cursor.strftime("%Y-%m"))
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        cursor = next_month
    return seq


@dataclass
class PeriodOutputs:
    payload_path: Path
    html_path: Path
    deals_csv_path: Path
    stores_csv_path: Path
    owners_csv_path: Path


def pull_hubspot_data(token: str, report_date: datetime, output_path: Path) -> dict:
    print(f"Fetching pipeline stage labels for '{PIPELINE_ID}'...", file=sys.stderr)
    stage_labels = fetch_stage_labels(token)
    print(f"  -> {len(stage_labels)} stages found and role IDs validated.", file=sys.stderr)

    print(f"Fetching deals from pipeline '{PIPELINE_ID}'...", file=sys.stderr)
    deals = fetch_all_deals(token)
    print(f"  -> Retrieved {len(deals)} deals.", file=sys.stderr)

    print(f"Fetching stores from custom object '{STORE_OBJECT_TYPE}'...", file=sys.stderr)
    stores = fetch_all_stores(token)
    print(f"  -> Retrieved {len(stores)} stores.", file=sys.stderr)
    if stores:
        by_status, test_count = summarize_stores(stores)
        print(f"  -> Status breakdown (excluding {test_count} test records):", file=sys.stderr)
        for status, count in sorted(by_status.items(), key=lambda kv: -kv[1]):
            print(f"      {status:20s}  {count:>5d}", file=sys.stderr)

    print("Fetching HubSpot owner roster...", file=sys.stderr)
    owners = fetch_owner_map(token)
    print(f"  -> Retrieved {len(owners)} owner id/name mappings.", file=sys.stderr)

    payload = {
        "report_type": "truage_activation_period_report",
        "pulled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_date": report_date.strftime("%Y-%m-%d"),
        "pipeline": PIPELINE_ID,
        "pipeline_label": "Retailer Activations",
        "stage_labels": stage_labels,
        "active_stage_ids": STAGE_ROLES["active_stage_ids"],
        "in_lab_stage_id": STAGE_ROLES["in_lab_stage_id"],
        "awaiting_sw_stage_id": STAGE_ROLES["awaiting_sw_stage_id"],
        "awaiting_activation_stage_id": STAGE_ROLES["awaiting_activation_stage_id"],
        "awaiting_transactions_stage_id": STAGE_ROLES["awaiting_transactions_stage_id"],
        "onboarding_stage_id": STAGE_ROLES["onboarding_stage_id"],
        "store_object_type_id": STORE_OBJECT_TYPE,
        "owners": owners,
        "deals": deals,
        "stores": stores,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def compute_period_metrics(payload: dict, start_date: datetime, end_date: datetime) -> dict:
    deals = payload["deals"]
    stores = payload["stores"]
    owner_map = payload.get("owners", {})
    real_stores = [
        s for s in stores
        if not is_test_store(s)
        and not contains_excluded_token(
            s.get("legal_name"),
            s.get("store_brand_name"),
            s.get("external_id"),
            s.get("organization_id"),
            s.get("status"),
        )
    ]
    test_filtered_deals = [
        d for d in deals
        if not is_test_deal_name(d.get("dealname") or "")
        and not contains_excluded_token(
            d.get("dealname"),
            d.get("blocked_reason"),
        )
    ]

    period_deals = []
    for deal in test_filtered_deals:
        closed_ts = deal_closedwon_date(deal)
        if in_period(closed_ts, start_date, end_date):
            period_deals.append({
                "id": deal.get("id"),
                "dealname": (deal.get("dealname") or "").strip(),
                "dealstage": deal.get("dealstage") or "",
                "amount": safe_int(deal.get("amount")),
                "owner": owner_name(deal.get("hubspot_owner_id"), owner_map),
                "closedwon_at": closed_ts,
                "createdate": parse_dt(deal.get("createdate")),
                "blocked_reason": (deal.get("blocked_reason") or "").strip(),
            })

    period_stores = []
    for store in real_stores:
        activated_ts = store_activated_date(store)
        if in_period(activated_ts, start_date, end_date):
            period_stores.append({
                "id": store.get("id"),
                "legal_name": (store.get("legal_name") or "").strip(),
                "store_brand_name": (store.get("store_brand_name") or "").strip(),
                "owner": owner_name(store.get("hubspot_owner_id"), owner_map),
                "status": normalize_status(store),
                "activated_at": activated_ts,
                "lasttransactiondate": parse_dt(store.get("lasttransactiondate")),
                "hs_createdate": parse_dt(store.get("hs_createdate")),
            })

    current_status_counter = Counter(normalize_status(store) for store in real_stores)
    active_stage_ids = set(payload["active_stage_ids"])
    current_active_deals = [
        d for d in test_filtered_deals
        if (d.get("dealstage") or "") in active_stage_ids
    ]

    monthly_buckets = build_month_sequence(start_date, end_date)
    monthly = {
        bucket: {
            "label": month_bucket_label(bucket, start_date, end_date),
            "activated_stores": 0,
            "closedwon_deals": 0,
            "closedwon_amount": 0,
        }
        for bucket in monthly_buckets
    }
    for store in period_stores:
        bucket = month_bucket(store["activated_at"])
        if bucket in monthly:
            monthly[bucket]["activated_stores"] += 1
    for deal in period_deals:
        bucket = month_bucket(deal["closedwon_at"])
        if bucket in monthly:
            monthly[bucket]["closedwon_deals"] += 1
            monthly[bucket]["closedwon_amount"] += deal["amount"]

    owner_rows = defaultdict(lambda: {
        "owner": "",
        "activated_stores_in_period": 0,
        "closedwon_deals_in_period": 0,
        "closedwon_amount_in_period": 0,
        "current_active_store_count": 0,
        "current_pending_store_count": 0,
        "current_ready_store_count": 0,
    })
    for store in period_stores:
        row = owner_rows[store["owner"]]
        row["owner"] = store["owner"]
        row["activated_stores_in_period"] += 1
    for deal in period_deals:
        row = owner_rows[deal["owner"]]
        row["owner"] = deal["owner"]
        row["closedwon_deals_in_period"] += 1
        row["closedwon_amount_in_period"] += deal["amount"]
    for store in real_stores:
        owner = owner_name(store.get("hubspot_owner_id"), owner_map)
        row = owner_rows[owner]
        row["owner"] = owner
        status = normalize_status(store)
        if status == "Active":
            row["current_active_store_count"] += 1
        elif status == "Pending":
            row["current_pending_store_count"] += 1
        elif status == "Ready":
            row["current_ready_store_count"] += 1

    owner_summary = sorted(
        owner_rows.values(),
        key=lambda row: (
            -row["activated_stores_in_period"],
            -row["closedwon_deals_in_period"],
            row["owner"],
        ),
    )

    return {
        "start_date": start_date,
        "end_date": end_date,
        "pulled_at": parse_dt(payload.get("pulled_at")) or end_date,
        "period_deals": sorted(period_deals, key=lambda d: d["closedwon_at"], reverse=True),
        "period_stores": sorted(period_stores, key=lambda s: s["activated_at"], reverse=True),
        "owner_summary": owner_summary,
        "monthly": [monthly[b] for b in monthly_buckets],
        "current_status_counter": current_status_counter,
        "summary": {
            "activated_stores_in_period": len(period_stores),
            "closedwon_deals_in_period": len(period_deals),
            "closedwon_amount_in_period": sum(d["amount"] for d in period_deals),
            "current_active_stores": current_status_counter.get("Active", 0),
            "current_pending_stores": current_status_counter.get("Pending", 0),
            "current_ready_stores": current_status_counter.get("Ready", 0),
            "current_total_real_stores": len(real_stores),
            "current_active_deal_count": len(current_active_deals),
        },
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            cooked = {}
            for key in fieldnames:
                value = row.get(key)
                if isinstance(value, datetime):
                    cooked[key] = value.isoformat()
                else:
                    cooked[key] = value
            writer.writerow(cooked)


def render_html(metrics: dict) -> str:
    summary = metrics["summary"]
    start_label = fmt_date(metrics["start_date"], "%B %-d, %Y")
    end_label = fmt_date(metrics["end_date"], "%B %-d, %Y")
    pulled_label = fmt_date(metrics["pulled_at"], "%b %-d, %Y") + " " + metrics["pulled_at"].strftime("%H:%M UTC")

    monthly_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['label'])}</td>
          <td class="num">{row['activated_stores']:,}</td>
          <td class="num">{row['closedwon_deals']:,}</td>
          <td class="num">{row['closedwon_amount']:,}</td>
        </tr>
        """
        for row in metrics["monthly"]
    )

    owner_rows = "".join(
        f"""
        <tr>
          <td>{escape(row['owner'])}</td>
          <td class="num">{row['activated_stores_in_period']:,}</td>
          <td class="num">{row['closedwon_deals_in_period']:,}</td>
          <td class="num">{row['closedwon_amount_in_period']:,}</td>
          <td class="num">{row['current_active_store_count']:,}</td>
          <td class="num">{row['current_pending_store_count']:,}</td>
          <td class="num">{row['current_ready_store_count']:,}</td>
        </tr>
        """
        for row in metrics["owner_summary"]
    ) or '<tr><td colspan="7">No owner-level activity found in this period.</td></tr>'

    activated_rows = "".join(
        f"""
        <tr>
          <td>{store['activated_at'].strftime('%Y-%m-%d')}</td>
          <td>{escape(store['legal_name'] or '(No legal name)')}</td>
          <td>{escape(store['store_brand_name'] or '')}</td>
          <td>{escape(store['owner'])}</td>
          <td>{escape(store['status'])}</td>
        </tr>
        """
        for store in metrics["period_stores"][:250]
    ) or '<tr><td colspan="5">No activated stores were found in this period.</td></tr>'

    closed_rows = "".join(
        f"""
        <tr>
          <td>{deal['closedwon_at'].strftime('%Y-%m-%d')}</td>
          <td>{escape(deal['dealname'] or '(No deal name)')}</td>
          <td class="num">{deal['amount']:,}</td>
          <td>{escape(deal['owner'])}</td>
          <td>{escape(deal['dealstage'])}</td>
          <td>{escape(deal['blocked_reason'] or '')}</td>
        </tr>
        """
        for deal in metrics["period_deals"][:250]
    ) or '<tr><td colspan="6">No closed-won deals were found in this period.</td></tr>'

    status_rows = "".join(
        f"""
        <tr>
          <td>{escape(status)}</td>
          <td class="num">{count:,}</td>
        </tr>
        """
        for status, count in sorted(metrics["current_status_counter"].items(), key=lambda item: (-item[1], item[0]))
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TruAge Activation Period Report</title>
  <style>
    :root {{
      --navy: #00203F;
      --teal: #36ECDE;
      --bg: #F5F0E8;
      --card: #FFFFFF;
      --line: #DDD8CE;
      --muted: #6C6A68;
      --ink: #172331;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }}
    .page {{
      max-width: 1240px;
      margin: 0 auto;
      padding: 28px 24px 48px;
    }}
    .hero {{
      background: linear-gradient(135deg, var(--navy), #11355E);
      color: white;
      border-radius: 18px;
      padding: 24px 28px;
      box-shadow: 0 10px 30px rgba(0, 32, 63, 0.18);
      margin-bottom: 22px;
    }}
    .eyebrow {{
      color: var(--teal);
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 10px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 34px;
      line-height: 1.1;
    }}
    .sub {{
      color: rgba(255,255,255,0.82);
      font-size: 15px;
      max-width: 900px;
      line-height: 1.5;
    }}
    .meta {{
      margin-top: 14px;
      font-size: 13px;
      color: rgba(255,255,255,0.72);
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 16px;
      margin-bottom: 22px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px 18px 16px;
      box-shadow: 0 4px 18px rgba(0, 32, 63, 0.05);
    }}
    .stat-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 8px;
    }}
    .stat-value {{
      font-size: 32px;
      font-weight: 800;
      line-height: 1;
      margin-bottom: 6px;
    }}
    .stat-sub {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }}
    .section {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 16px;
      padding: 18px;
      margin-bottom: 18px;
      box-shadow: 0 4px 18px rgba(0, 32, 63, 0.05);
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 20px;
    }}
    p.note {{
      margin: 0 0 12px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.55;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      padding: 10px 12px;
      border-top: 1px solid #ECE7DE;
      vertical-align: top;
      text-align: left;
    }}
    thead th {{
      border-top: none;
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .num {{
      text-align: right;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }}
    .two-col {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 18px;
    }}
    .foot {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 10px;
    }}
    @media (max-width: 980px) {{
      .grid, .two-col {{
        grid-template-columns: 1fr;
      }}
      .stat-value {{
        font-size: 28px;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <div class="eyebrow">TruAge Activation Period Report</div>
      <h1>{escape(start_label)} through {escape(end_label)}</h1>
      <div class="sub">
        Additive fixed-window report. This view measures stores activated during the period,
        deals entering closed-won during the period, and the current store status snapshot
        as of the live HubSpot pull.
      </div>
      <div class="meta">Pulled from HubSpot: {escape(pulled_label)}</div>
    </section>

    <section class="grid">
      <div class="card">
        <div class="stat-label">Activated Stores In Period</div>
        <div class="stat-value">{summary['activated_stores_in_period']:,}</div>
        <div class="stat-sub">Store records with <code>activated_at</code> inside the requested date window.</div>
      </div>
      <div class="card">
        <div class="stat-label">Closed-Won Deals In Period</div>
        <div class="stat-value">{summary['closedwon_deals_in_period']:,}</div>
        <div class="stat-sub">Deals using <code>hs_v2_date_entered_closedwon</code>, with <code>closedate</code> as fallback.</div>
      </div>
      <div class="card">
        <div class="stat-label">Closed-Won Store-Count Value</div>
        <div class="stat-value">{summary['closedwon_amount_in_period']:,}</div>
        <div class="stat-sub">Summed from deal <code>amount</code> in the reporting window. This is store-count/opportunity value, not revenue dollars.</div>
      </div>
      <div class="card">
        <div class="stat-label">Current Active Stores</div>
        <div class="stat-value">{summary['current_active_stores']:,}</div>
        <div class="stat-sub">Current snapshot from the Stores object at pull time.</div>
      </div>
      <div class="card">
        <div class="stat-label">Current Pending Stores</div>
        <div class="stat-value">{summary['current_pending_stores']:,}</div>
        <div class="stat-sub">Current snapshot from the Stores object at pull time.</div>
      </div>
      <div class="card">
        <div class="stat-label">Current Ready Stores</div>
        <div class="stat-value">{summary['current_ready_stores']:,}</div>
        <div class="stat-sub">Current snapshot from the Stores object at pull time.</div>
      </div>
    </section>

    <section class="section">
      <h2>Monthly Trend</h2>
      <p class="note">These are period-window buckets, not week-over-week comparisons.</p>
      <table>
        <thead>
          <tr>
            <th>Month</th>
            <th class="num">Activated Stores</th>
            <th class="num">Closed-Won Deals</th>
            <th class="num">Closed-Won Store-Count Value</th>
          </tr>
        </thead>
        <tbody>{monthly_rows}</tbody>
      </table>
    </section>

    <section class="section">
      <h2>Owner Performance</h2>
      <table>
        <thead>
          <tr>
            <th>Owner</th>
            <th class="num">Activated Stores</th>
            <th class="num">Closed-Won Deals</th>
            <th class="num">Closed-Won Store-Count Value</th>
            <th class="num">Current Active</th>
            <th class="num">Current Pending</th>
            <th class="num">Current Ready</th>
          </tr>
        </thead>
        <tbody>{owner_rows}</tbody>
      </table>
    </section>

    <section class="two-col">
      <div class="section">
        <h2>Activated Store Detail</h2>
      <p class="note">Showing up to 250 most recent activations in the requested period after test and lab-style records are scrubbed. Full detail is also in CSV.</p>
        <table>
          <thead>
            <tr>
              <th>Activated</th>
              <th>Legal Name</th>
              <th>Brand</th>
              <th>Owner</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>{activated_rows}</tbody>
        </table>
      </div>
      <div class="section">
        <h2>Current Status Snapshot</h2>
        <p class="note">This is a pull-time snapshot, not a reconstructed historical status baseline.</p>
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th class="num">Count</th>
            </tr>
          </thead>
          <tbody>{status_rows}</tbody>
        </table>
        <div class="foot">Current real stores counted: {summary['current_total_real_stores']:,}</div>
      </div>
    </section>

    <section class="section">
      <h2>Closed-Won Deal Detail</h2>
      <p class="note">Showing up to 250 most recent closed-won deals in the requested period. The value column reflects store-count/opportunity sizing, not revenue dollars.</p>
      <table>
        <thead>
          <tr>
            <th>Closed-Won Date</th>
            <th>Deal Name</th>
            <th class="num">Amount</th>
            <th>Owner</th>
            <th>Current Stage</th>
            <th>Blocked Reason</th>
          </tr>
        </thead>
        <tbody>{closed_rows}</tbody>
      </table>
    </section>
  </div>
</body>
</html>"""


def write_outputs(output_dir: Path, payload: dict, metrics: dict) -> PeriodOutputs:
    output_dir.mkdir(parents=True, exist_ok=True)
    start_slug = slug_date(metrics["start_date"])
    end_slug = slug_date(metrics["end_date"])
    payload_path = output_dir / f"hubspot_period_pull_{start_slug}_to_{end_slug}.json"
    html_path = output_dir / f"TruAge_Activation_Period_Report_{start_slug}_to_{end_slug}.html"
    deals_csv_path = output_dir / f"closedwon_deals_{start_slug}_to_{end_slug}.csv"
    stores_csv_path = output_dir / f"activated_stores_{start_slug}_to_{end_slug}.csv"
    owners_csv_path = output_dir / f"owner_summary_{start_slug}_to_{end_slug}.csv"

    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    html_path.write_text(render_html(metrics), encoding="utf-8")
    write_csv(
        deals_csv_path,
        ["closedwon_at", "id", "dealname", "amount", "owner", "dealstage", "createdate", "blocked_reason"],
        metrics["period_deals"],
    )
    write_csv(
        stores_csv_path,
        ["activated_at", "id", "legal_name", "store_brand_name", "owner", "status", "lasttransactiondate", "hs_createdate"],
        metrics["period_stores"],
    )
    write_csv(
        owners_csv_path,
        [
            "owner",
            "activated_stores_in_period",
            "closedwon_deals_in_period",
            "closedwon_amount_in_period",
            "current_active_store_count",
            "current_pending_store_count",
            "current_ready_store_count",
        ],
        metrics["owner_summary"],
    )

    return PeriodOutputs(
        payload_path=payload_path,
        html_path=html_path,
        deals_csv_path=deals_csv_path,
        stores_csv_path=stores_csv_path,
        owners_csv_path=owners_csv_path,
    )


def parse_iso_day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate additive TruAge period report")
    parser.add_argument("--start-date", default="2026-01-01", help="Period start date YYYY-MM-DD")
    parser.add_argument("--end-date", default="2026-05-20", help="Period end date YYYY-MM-DD")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--token", default=None, help="HubSpot token or use HUBSPOT_TOKEN")
    args = parser.parse_args()

    token = args.token or os.environ.get("HUBSPOT_TOKEN")
    if not token:
        print("ERROR: No token. Set HUBSPOT_TOKEN env var or pass --token.", file=sys.stderr)
        sys.exit(1)

    start_date = parse_iso_day(args.start_date)
    end_date = parse_iso_day(args.end_date).replace(hour=23, minute=59, second=59)
    if end_date < start_date:
        print("ERROR: end date must be on or after start date.", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(args.output_dir) if args.output_dir else default_output_dir(start_date, end_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_temp = output_dir / "_period_pull.tmp.json"
    payload = pull_hubspot_data(token, end_date, payload_temp)
    metrics = compute_period_metrics(payload, start_date, end_date)
    outputs = write_outputs(output_dir, payload, metrics)
    if payload_temp.exists():
        payload_temp.unlink()

    summary = metrics["summary"]
    print("")
    print(f"Period report complete: {fmt_date(start_date)} through {fmt_date(end_date)}")
    print(f"Activated stores in period: {summary['activated_stores_in_period']:,}")
    print(f"Closed-won deals in period: {summary['closedwon_deals_in_period']:,}")
    print(f"Closed-won amount in period: {summary['closedwon_amount_in_period']:,}")
    print(f"Current active / pending / ready: {summary['current_active_stores']:,} / {summary['current_pending_stores']:,} / {summary['current_ready_stores']:,}")
    print("")
    print(f"HTML report:      {outputs.html_path}")
    print(f"Raw pull JSON:    {outputs.payload_path}")
    print(f"Deals CSV:        {outputs.deals_csv_path}")
    print(f"Stores CSV:       {outputs.stores_csv_path}")
    print(f"Owner summary:    {outputs.owners_csv_path}")


if __name__ == "__main__":
    main()
