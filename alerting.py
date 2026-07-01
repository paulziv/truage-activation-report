"""
Crash alerting via Resend — the same provider pez-portal already uses
successfully to send the actual report emails, so this doesn't depend on a
separate, unconfirmed Postmark setup. No-ops gracefully (logs only) when
RESEND_API_KEY isn't set on this service. Set RESEND_API_KEY (and optionally
RESEND_FROM_EMAIL) in this service's Railway environment to turn alerts on.

Alerts are rate-limited per error "kind" (e.g. "fetch_failed", "generate_failed")
so a crash-looping service doesn't spam the inbox — at most one email per kind
every ALERT_COOLDOWN_SECONDS (default 30 min).
"""
import os
import time
import logging
import traceback

import requests

log = logging.getLogger("truage-activation.alerting")

RESEND_URL = "https://api.resend.com/emails"
ALERT_TO = os.environ.get("ALERT_EMAIL", "ziv.paul@gmail.com")
ALERT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "1800"))

_last_sent: dict[str, float] = {}


def send_crash_alert(kind: str, message: str, exc: Exception | None = None) -> None:
    """Email ALERT_TO that the pipeline crashed. `kind` is a short stable
    identifier for the failure type (used for rate-limiting), e.g.
    'fetch_failed' or 'generate_failed'."""
    now = time.time()
    last = _last_sent.get(kind, 0)
    if now - last < ALERT_COOLDOWN_SECONDS:
        log.info(
            "Alert for %r suppressed (sent %.0fs ago, cooldown %ds)",
            kind, now - last, ALERT_COOLDOWN_SECONDS,
        )
        return

    api_key = os.environ.get("RESEND_API_KEY")
    from_email = os.environ.get("RESEND_FROM_EMAIL", "alerts@mytruage.org")

    body_lines = [
        f"<p><b>TruAge Activation Report</b> pipeline failed.</p>",
        f"<p><b>Kind:</b> {kind}</p>",
        f"<p><b>Message:</b> {message}</p>",
    ]
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        body_lines.append(f"<pre style='white-space:pre-wrap;font-size:.8rem'>{tb}</pre>")
    html_body = "\n".join(body_lines)

    if not api_key:
        log.warning(
            "RESEND_API_KEY not set — crash alert NOT sent (would have gone to %s). "
            "Kind=%s Message=%s", ALERT_TO, kind, message,
        )
        return

    try:
        resp = requests.post(
            RESEND_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_email,
                "to": [ALERT_TO],
                "subject": f"[TruAge Activation] Pipeline failure: {kind}",
                "html": html_body,
            },
            timeout=15,
        )
        resp.raise_for_status()
        _last_sent[kind] = now
        log.info("Crash alert sent to %s for kind=%s", ALERT_TO, kind)
    except Exception as send_exc:
        # Never let alerting itself crash the pipeline's error handler.
        log.error("Failed to send crash alert: %s", send_exc)
