"""
Structured run history for the report pipeline — persisted in Postgres
(DATABASE_URL) so it survives redeploys; falls back to local SQLite for
dev when DATABASE_URL isn't set. This lets you review what happened across
recent runs ("/history") independent of scrolling through raw Railway logs.

Same DB-backend pattern as the truage-pulse repo's pulse/storage.py.
"""
import os
import sqlite3
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("truage-activation.history")

DB_PATH = Path(__file__).resolve().parent / "data" / "run_history.db"


def _is_postgres() -> bool:
    return os.environ.get("DATABASE_URL", "").startswith("postgres")


def _ph() -> str:
    return "%s" if _is_postgres() else "?"


@contextmanager
def _get_conn():
    if _is_postgres():
        import psycopg2  # lazy import — not needed for local dev
        conn = psycopg2.connect(os.environ["DATABASE_URL"])
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def _ensure_table() -> None:
    pg = _is_postgres()
    serial = "SERIAL" if pg else "INTEGER"
    autoincrement = "" if pg else " AUTOINCREMENT"
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS run_history (
                    id {serial} PRIMARY KEY{autoincrement},
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    step TEXT,
                    error TEXT
                )
            """)
    except Exception as exc:
        log.warning("run_history: could not ensure table exists: %s", exc)


_ensure_table()


def record_run(
    *,
    status: str,
    duration_seconds: float,
    step: str | None = None,
    error: str | None = None,
) -> None:
    """Append one run record. Best-effort — never raises."""
    ph = _ph()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                f"INSERT INTO run_history (timestamp, status, duration_seconds, step, error) "
                f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph})",
                (timestamp, status, round(duration_seconds, 2), step, (error or "")[:4000] or None),
            )
    except Exception as exc:
        log.warning("Could not write run history: %s", exc)


def recent_runs(limit: int = 50) -> list[dict]:
    """Most recent runs first."""
    try:
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT timestamp, status, duration_seconds, step, error FROM run_history "
                "ORDER BY id DESC LIMIT " + str(int(limit))
            )
            rows = cur.fetchall()
            return [
                {
                    "timestamp":         r[0] if isinstance(r, tuple) else r["timestamp"],
                    "status":            r[1] if isinstance(r, tuple) else r["status"],
                    "duration_seconds":  r[2] if isinstance(r, tuple) else r["duration_seconds"],
                    "step":              r[3] if isinstance(r, tuple) else r["step"],
                    "error":             r[4] if isinstance(r, tuple) else r["error"],
                }
                for r in rows
            ]
    except Exception as exc:
        log.warning("Could not read run history: %s", exc)
        return []
