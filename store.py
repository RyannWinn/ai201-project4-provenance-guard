"""
store.py — SQLite-backed content store and structured audit log.

Two tables:
  content : the current state of each submission (used by /appeal to flip status)
  audit   : an append-only structured log of every event (submissions + appeals)

The audit log is intentionally the source of truth for "what happened when".
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).with_name("provenance.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content (
                content_id      TEXT PRIMARY KEY,
                creator_id      TEXT NOT NULL,
                text            TEXT NOT NULL,
                attribution     TEXT NOT NULL,
                confidence      REAL NOT NULL,
                llm_score       REAL,
                style_score     REAL NOT NULL,
                status          TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id        TEXT NOT NULL,
                creator_id        TEXT,
                event             TEXT NOT NULL,
                timestamp         TEXT NOT NULL,
                attribution       TEXT,
                confidence        REAL,
                llm_score         REAL,
                style_score       REAL,
                status            TEXT,
                appeal_reasoning  TEXT
            )
            """
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_submission(content_id: str, creator_id: str, text: str, result) -> dict:
    """Persist a new submission and write its audit entry. `result` is a SignalResult."""
    ts = _now()
    llm = None if result.llm_score < 0 else result.llm_score
    with _conn() as conn:
        conn.execute(
            """INSERT INTO content
               (content_id, creator_id, text, attribution, confidence,
                llm_score, style_score, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (content_id, creator_id, text, result.attribution, result.confidence,
             llm, result.style_score, "classified", ts),
        )
        conn.execute(
            """INSERT INTO audit
               (content_id, creator_id, event, timestamp, attribution,
                confidence, llm_score, style_score, status, appeal_reasoning)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (content_id, creator_id, "submission", ts, result.attribution,
             result.confidence, llm, result.style_score, "classified", None),
        )
    return {"content_id": content_id, "timestamp": ts, "status": "classified"}


def get_content(content_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM content WHERE content_id = ?", (content_id,)
        ).fetchone()
    return dict(row) if row else None


def record_appeal(content_id: str, creator_reasoning: str) -> dict | None:
    """Flip status to under_review and log the appeal. Returns None if unknown id."""
    content = get_content(content_id)
    if content is None:
        return None
    ts = _now()
    with _conn() as conn:
        conn.execute(
            "UPDATE content SET status = ? WHERE content_id = ?",
            ("under_review", content_id),
        )
        conn.execute(
            """INSERT INTO audit
               (content_id, creator_id, event, timestamp, attribution,
                confidence, llm_score, style_score, status, appeal_reasoning)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (content_id, content["creator_id"], "appeal", ts, content["attribution"],
             content["confidence"], content["llm_score"], content["style_score"],
             "under_review", creator_reasoning),
        )
    return {"content_id": content_id, "status": "under_review", "timestamp": ts}


def get_log(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_appeal_queue() -> list[dict]:
    """What a human reviewer sees: everything currently under review."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM content WHERE status = 'under_review' ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]
