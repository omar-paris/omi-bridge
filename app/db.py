"""SQLite — stockage segments, memories, day summaries, commandes."""
import json
import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    uid TEXT,
    seg_key TEXT NOT NULL,
    text TEXT NOT NULL,
    speaker TEXT,
    is_user INTEGER,
    start REAL,
    end REAL,
    received_at TEXT NOT NULL,
    UNIQUE(session_id, seg_key)
);
CREATE INDEX IF NOT EXISTS idx_segments_session ON segments(session_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    omi_id TEXT,
    title TEXT,
    overview TEXT,
    category TEXT,
    raw TEXT NOT NULL,
    created_at TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS day_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT,
    raw TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hermes_sessions (
    uid TEXT NOT NULL,
    agent TEXT NOT NULL,
    hermes_session_id TEXT NOT NULL,
    last_used TEXT NOT NULL,
    PRIMARY KEY (uid, agent)
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    uid TEXT,
    agent TEXT,
    command_text TEXT,
    context_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    response_text TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def insert_segment(session_id: str, uid: str, seg: dict) -> bool:
    """Insère un segment, retourne False si doublon (déjà reçu)."""
    seg_key = str(seg.get("id") or f"{seg.get('start')}|{seg.get('text', '')[:80]}")
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO segments "
            "(session_id, uid, seg_key, text, speaker, is_user, start, end, received_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id, uid, seg_key,
                seg.get("text", ""), seg.get("speaker"),
                1 if seg.get("is_user") else 0,
                seg.get("start"), seg.get("end"), now_iso(),
            ),
        )
        return cur.rowcount > 0


def recent_segments(session_id: str, limit: int) -> list[sqlite3.Row]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM segments WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return list(reversed(rows))


def insert_memory(uid: str, payload: dict) -> None:
    structured = payload.get("structured") or {}
    with connect() as conn:
        conn.execute(
            "INSERT INTO memories (uid, omi_id, title, overview, category, raw, created_at, received_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                uid, str(payload.get("id", "")),
                structured.get("title"), structured.get("overview"),
                structured.get("category"),
                json.dumps(payload, ensure_ascii=False),
                payload.get("created_at"), now_iso(),
            ),
        )


def insert_day_summary(uid: str, payload: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO day_summaries (uid, raw, received_at) VALUES (?,?,?)",
            (uid, json.dumps(payload, ensure_ascii=False), now_iso()),
        )


def create_command(session_id: str, uid: str, agent: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO commands (session_id, uid, agent, created_at) VALUES (?,?,?,?)",
            (session_id, uid, agent, now_iso()),
        )
        return cur.lastrowid


def finalize_command(cmd_id: int, command_text: str, context_text: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE commands SET command_text=?, context_text=?, status='dispatched' WHERE id=?",
            (command_text, context_text, cmd_id),
        )


def complete_command(cmd_id: int, status: str, response_text: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE commands SET status=?, response_text=?, completed_at=? WHERE id=?",
            (status, response_text, now_iso(), cmd_id),
        )


def get_hermes_session(uid: str, agent: str, max_age_hours: float) -> str | None:
    """Session hermes à reprendre si la dernière commande est assez récente."""
    with connect() as conn:
        row = conn.execute(
            "SELECT hermes_session_id FROM hermes_sessions "
            "WHERE uid=? AND agent=? AND last_used > datetime('now', ?)",
            (uid, agent, f"-{max_age_hours} hours"),
        ).fetchone()
    return row["hermes_session_id"] if row else None


def save_hermes_session(uid: str, agent: str, session_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO hermes_sessions (uid, agent, hermes_session_id, last_used) "
            "VALUES (?,?,?,?) ON CONFLICT(uid, agent) DO UPDATE SET "
            "hermes_session_id=excluded.hermes_session_id, last_used=excluded.last_used",
            (uid, agent, session_id, now_iso().replace("T", " ")[:19]),
        )


def stats() -> dict:
    with connect() as conn:
        return {
            "segments": conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"],
            "memories": conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"],
            "day_summaries": conn.execute("SELECT COUNT(*) c FROM day_summaries").fetchone()["c"],
            "commands": conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"],
        }
