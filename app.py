"""
TruAge Activation Report — Flask web service for Railway deployment.

Routes:
  /          Serves the latest generated report HTML
  /refresh   POST — triggers a fresh HubSpot pull + report generation
  /status    GET  — JSON status of the last run
  /health    GET  — liveness check for Railway
"""
import os
import subprocess
import threading
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, render_template_string
from dotenv import load_dotenv

import alerting
import run_history

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("truage-activation")

app = Flask(__name__)

# Paths — use /tmp so they survive between requests but reset on redeploy (fine for reports)
BASE_DIR   = Path(__file__).resolve().parent
PULL_PATH  = Path("/tmp/hubspot_pull.json")
REPORT_PATH = Path("/tmp/latest_report.html")

_lock = threading.Lock()
_last_run: dict = {"status": "never", "timestamp": None, "error": None}


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline() -> None:
    """Fetch from HubSpot then generate the HTML report. Thread-safe."""
    global _last_run
    with _lock:
        _last_run["status"] = "running"
        log.info("Pipeline starting…")
        start = time.monotonic()
        try:
            # Step 1: fetch
            fetch = subprocess.run(
                ["python", str(BASE_DIR / "fetch_from_hubspot.py"),
                 "--output", str(PULL_PATH)],
                capture_output=True, text=True, timeout=120,
            )
            if fetch.returncode != 0:
                raise RuntimeError(f"Fetch failed:\n{fetch.stderr}")
            log.info("Fetch complete. Generating report…")

            # Step 2: generate HTML
            gen = subprocess.run(
                ["python", str(BASE_DIR / "generate_report_html.py"),
                 "--input", str(PULL_PATH),
                 "--output", str(REPORT_PATH)],
                capture_output=True, text=True, timeout=120,
            )
            if gen.returncode != 0:
                raise RuntimeError(f"Generate failed:\n{gen.stderr}")

            _last_run["status"]    = "ok"
            _last_run["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            _last_run["error"]     = None
            log.info("Report generated: %s", REPORT_PATH)
            run_history.record_run(
                status="ok",
                duration_seconds=time.monotonic() - start,
            )

        except Exception as exc:
            _last_run["status"] = "error"
            _last_run["error"]  = str(exc)
            log.error("Pipeline error: %s", exc)

            msg = str(exc)
            if msg.startswith("Fetch failed"):
                kind = "fetch_failed"
            elif msg.startswith("Generate failed"):
                kind = "generate_failed"
            else:
                kind = "unexpected_error"

            run_history.record_run(
                status="error",
                duration_seconds=time.monotonic() - start,
                step=kind,
                error=msg[:4000],  # keep history entries bounded
            )
            alerting.send_crash_alert(kind, msg, exc)


# ── Auto-run on startup ───────────────────────────────────────────────────────

def _startup_run() -> None:
    """Run the pipeline once at startup so the report is ready immediately."""
    if not os.environ.get("HUBSPOT_TOKEN"):
        log.warning("HUBSPOT_TOKEN not set — skipping startup run.")
        return
    run_pipeline()


threading.Thread(target=_startup_run, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

STATUS_PAGE = """
<!doctype html>
<html>
<head>
  <title>TruAge Activation Report</title>
  <style>
    body { font-family: sans-serif; max-width: 640px; margin: 80px auto; color: #333; }
    h1   { font-size: 1.4rem; }
    .badge { display:inline-block; padding:4px 10px; border-radius:4px; font-size:.85rem; }
    .never  { background:#eee; }
    .running{ background:#fff3cd; }
    .ok     { background:#d4edda; }
    .error  { background:#f8d7da; }
    pre { background:#f4f4f4; padding:12px; border-radius:4px; font-size:.8rem; white-space:pre-wrap; }
    form { margin-top:24px; }
    button { padding:8px 18px; background:#0f4c81; color:#fff; border:none; border-radius:4px; cursor:pointer; }
  </style>
</head>
<body>
  <h1>TruAge Activation Report</h1>
  <p>Status: <span class="badge {{ last_run.status }}">{{ last_run.status }}</span></p>
  {% if last_run.timestamp %}<p>Last run: {{ last_run.timestamp }}</p>{% endif %}
  {% if last_run.error %}<pre>{{ last_run.error }}</pre>{% endif %}
  {% if last_run.status == 'running' %}
    <p>Report is being generated — refresh this page in a moment.</p>
  {% else %}
    <form method="POST" action="/refresh">
      <button type="submit">Generate Report Now</button>
    </form>
  {% endif %}
</body>
</html>
"""


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/status")
def status():
    return jsonify(_last_run)


@app.route("/history")
def history():
    """Recent pipeline runs (success/failure, timing, errors) — for reviewing
    what happened across recent runs beyond what's convenient to scroll through
    in raw Railway logs. Resets on redeploy (stored in /tmp)."""
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(run_history.recent_runs(limit=limit))


@app.route("/")
def index():
    if REPORT_PATH.exists():
        return Response(REPORT_PATH.read_text(encoding="utf-8"), mimetype="text/html")
    # Report not ready yet — show status page
    return render_template_string(STATUS_PAGE, last_run=_last_run), 202


@app.route("/refresh", methods=["GET", "POST"])
def refresh():
    # Optional secret protection
    secret = os.environ.get("REFRESH_SECRET")
    if secret:
        provided = (
            request.headers.get("X-Refresh-Secret")
            or request.args.get("secret")
        )
        if provided != secret:
            return jsonify({"error": "unauthorized"}), 401

    if _last_run["status"] == "running":
        return jsonify({"status": "already_running"}), 202

    thread = threading.Thread(target=run_pipeline, daemon=True)
    thread.start()

    # If called from a browser form, redirect back to root
    if request.method == "POST" and "text/html" in request.headers.get("Accept", ""):
        from flask import redirect, url_for
        return redirect(url_for("index"))

    return jsonify({"status": "started"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
