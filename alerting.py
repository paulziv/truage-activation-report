"""
Crash alerting via the shared truage_core.email helper (Resend).

Sends from the unified alerts@ address (see truage_core.email); no per-service
Resend wiring needed beyond the shared RESEND_API_KEY. No-ops gracefully (logs
only) when the key isn't set.

Alerts are rate-limited per error "kind" (e.g. "fetch_failed", "generate_failed")
so a crash-looping service doesn't spam the inbox — at most one email per kind
every ALERT_COOLDOWN_SECONDS (default 30 min).
"""
import os
import time
import logging
import traceback

from truage_core import email as tcemail

log = logging.getLogger("truage-activation.alerting")

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

    body_lines = [
        "<p><b>TruAge Activation Report</b> pipeline failed.</p>",
        f"<p><b>Kind:</b> {kind}</p>",
        f"<p><b>Message:</b> {message}</p>",
    ]
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        body_lines.append(f"<pre style='white-space:pre-wrap;font-size:.8rem'>{tb}</pre>")
    html_body = "\n".join(body_lines)

    result = tcemail.send(
        to=ALERT_TO,
        subject=f"[TruAge Activation] Pipeline failure: {kind}",
        html=html_body,
        purpose="alerts",
    )
    if result.get("ok"):
        _last_sent[kind] = now
        log.info("Crash alert sent to %s for kind=%s", ALERT_TO, kind)
    else:
        # Never let alerting itself crash the pipeline's error handler.
        log.error("Failed to send crash alert: %s", result.get("error"))
