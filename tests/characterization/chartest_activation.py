#!/usr/bin/env python3
"""
Characterization harness — TruAge Activation Report.

Proves the report's OUTPUT is unchanged across a refactor (e.g. adopting truage-core).
Deterministic by construction: it computes from a FIXED `hubspot_pull.json` at a FIXED
`--asof`, so any diff is a real behavior change, not input drift.

Captures two artifacts per snapshot:
  - metrics.json : the full computed metrics dict M (minus the echoed input payload),
                   normalized (datetimes→ISO, sets→sorted, Deal→dict). This is the signal.
  - report.html  : render_html(M) with incidental timestamps neutralized.
  - input.sha256 : sha of the pull file, so a snapshot always records what it ran on.

USAGE
  # 1) Baseline BEFORE any change (use a real pull; archive/ copies work):
  python tests/characterization/chartest_activation.py snapshot \
      --pull archive/hubspot_pull_2026-07-01.json --asof 2026-07-01 --out tests/characterization/baseline

  # 2) After the refactor, snapshot again to a different dir:
  python tests/characterization/chartest_activation.py snapshot \
      --pull archive/hubspot_pull_2026-07-01.json --asof 2026-07-01 --out tests/characterization/candidate

  # 3) Gate: exits non-zero on ANY drift.
  python tests/characterization/chartest_activation.py compare \
      tests/characterization/baseline tests/characterization/candidate

Run from the repo root (so `generate_report_html` imports).
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys
from datetime import datetime, timezone, date
from pathlib import Path

# repo root on path (this file is tests/characterization/chartest_activation.py)
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import generate_report_html as G  # noqa: E402


# ── normalization ────────────────────────────────────────────────────────────
def _default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o, key=str)
    if isinstance(o, G.Deal):
        return {"id": o.id, "name": o.name, "stage": o.stage,
                "amount": o.amount, "amount_raw": o.amount_raw, "owner": o.owner}
    if isinstance(o, tuple):
        return list(o)
    # last resort: stable repr (flagged so we notice if something important lands here)
    return {"__repr__": repr(o)}


def _metrics_snapshot(M: dict) -> str:
    # Exclude the echoed input payload (that's the fixture, hashed separately) and
    # keys that are pure echoes of asof/pull time (deterministic here anyway).
    m = {k: v for k, v in M.items() if k not in ("payload",)}
    return json.dumps(m, default=_default, sort_keys=True, indent=2, ensure_ascii=False)


_TS_PATTERNS = [
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} (ET|UTC)"), "<TS>"),
    (re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}"), "<DATE>"),
]
def _html_normalize(html: str) -> str:
    for pat, repl in _TS_PATTERNS:
        html = pat.sub(repl, html)
    return html


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_snapshot(args) -> int:
    asof = datetime.strptime(args.asof, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    payload, deals = G.load_data(args.pull)
    M = G.compute_metrics(payload, deals, asof)
    if not M.get("has_store_data"):
        print("WARNING: pull has no store data — activation refuses to render in prod. "
              "Snapshot metrics only.", file=sys.stderr)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(_metrics_snapshot(M), encoding="utf-8")
    try:
        pulled_at_str = (G.parse_dt(payload.get("pulled_at")) or asof).strftime("%Y-%m-%d %H:%M UTC")
        html = _html_normalize(G.render_html(M, pulled_at_str, asof))
        (out / "report.html").write_text(html, encoding="utf-8")
    except Exception as exc:  # rendering is secondary to the metrics signal
        (out / "report.html").write_text(f"<!-- render failed: {exc!r} -->", encoding="utf-8")
        print(f"WARNING: render_html failed ({exc!r}); metrics.json still written.", file=sys.stderr)
    sha = hashlib.sha256(Path(args.pull).read_bytes()).hexdigest()
    (out / "input.sha256").write_text(f"{sha}  {Path(args.pull).name}\n", encoding="utf-8")
    print(f"snapshot → {out}  (asof={args.asof}, pull sha={sha[:12]}…)")
    return 0


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""

def cmd_compare(args) -> int:
    a, b = Path(args.baseline), Path(args.candidate)
    drift = []
    for name in ("metrics.json", "report.html"):
        if _read(a / name) != _read(b / name):
            drift.append(name)
    sa, sb = _read(a / "input.sha256"), _read(b / "input.sha256")
    if sa != sb:
        print(f"⚠  inputs differ — NOT a valid characterization comparison:\n   {a}: {sa.strip()}\n   {b}: {sb.strip()}", file=sys.stderr)
        return 2
    if not drift:
        print("✓ identical — no behavior drift (metrics + html match).")
        return 0
    print(f"✗ DRIFT in: {', '.join(drift)}")
    # show a compact metrics diff to pinpoint the change
    if "metrics.json" in drift:
        try:
            ma = json.loads(_read(a / "metrics.json")); mb = json.loads(_read(b / "metrics.json"))
            keys = sorted(set(ma) | set(mb))
            for k in keys:
                if ma.get(k) != mb.get(k):
                    va, vb = repr(ma.get(k))[:80], repr(mb.get(k))[:80]
                    print(f"    {k}:  {va}  →  {vb}")
        except Exception:
            print("    (metrics.json changed but could not be parsed for a field diff)")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Characterization harness for the Activation Report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--pull", required=True); s.add_argument("--asof", required=True); s.add_argument("--out", required=True); s.set_defaults(fn=cmd_snapshot)
    c = sub.add_parser("compare"); c.add_argument("baseline"); c.add_argument("candidate"); c.set_defaults(fn=cmd_compare)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
