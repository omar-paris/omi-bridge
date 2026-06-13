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

-- Sujets extraits : une ligne par conversation/épisode capté
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT NOT NULL,
    memory_omi_id TEXT,
    day TEXT,                 -- YYYY-MM-DD (jour local de début)
    start_time TEXT,          -- ISO
    end_time TEXT,            -- ISO
    duration_min INTEGER,
    content_type TEXT,        -- conversation|reunion|solo|media|ambiance|autre
    side TEXT,                -- business|perso|unknown
    persons TEXT,             -- JSON list
    subject TEXT,
    todo TEXT,                -- JSON list
    omi_title TEXT,
    omi_category TEXT,
    -- annotations manuelles (film, spectacle, note perso...)
    user_label TEXT,
    user_note TEXT,
    user_rating TEXT,
    annotated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(memory_omi_id)
);
CREATE INDEX IF NOT EXISTS idx_topics_day ON topics(uid, day);

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


def all_memories() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute("SELECT id, uid, omi_id, raw FROM memories ORDER BY id"))


def upsert_topic(t: dict) -> None:
    """Insère/actualise un sujet extrait (clé = memory_omi_id).
    Ne touche pas aux colonnes d'annotation manuelle si déjà annoté."""
    with connect() as conn:
        conn.execute(
            """INSERT INTO topics
            (uid, memory_omi_id, day, start_time, end_time, duration_min,
             content_type, side, persons, subject, todo, omi_title, omi_category, created_at)
            VALUES (:uid,:memory_omi_id,:day,:start_time,:end_time,:duration_min,
             :content_type,:side,:persons,:subject,:todo,:omi_title,:omi_category,:created_at)
            ON CONFLICT(memory_omi_id) DO UPDATE SET
              day=excluded.day, start_time=excluded.start_time, end_time=excluded.end_time,
              duration_min=excluded.duration_min, content_type=excluded.content_type,
              side=excluded.side, persons=excluded.persons, subject=excluded.subject,
              todo=excluded.todo, omi_title=excluded.omi_title, omi_category=excluded.omi_category""",
            t,
        )


def topics_for_day(uid: str, day: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(conn.execute(
            "SELECT * FROM topics WHERE uid=? AND day=? ORDER BY start_time",
            (uid, day),
        ))


def recent_annotatable_topic(uid: str, types: tuple[str, ...] = ("media", "ambiance", "autre")) -> sqlite3.Row | None:
    """Dernier sujet média non encore annoté (pour « annote le film d'hier »)."""
    placeholders = ",".join("?" * len(types))
    with connect() as conn:
        return conn.execute(
            f"SELECT * FROM topics WHERE uid=? AND content_type IN ({placeholders}) "
            f"AND annotated=0 ORDER BY start_time DESC LIMIT 1",
            (uid, *types),
        ).fetchone()


def annotate_topic(topic_id: int, label: str | None, note: str | None, rating: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE topics SET user_label=COALESCE(?,user_label), "
            "user_note=COALESCE(?,user_note), user_rating=COALESCE(?,user_rating), "
            "annotated=1 WHERE id=?",
            (label, note, rating, topic_id),
        )


def stats() -> dict:
    with connect() as conn:
        return {
            "segments": conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"],
            "memories": conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"],
            "day_summaries": conn.execute("SELECT COUNT(*) c FROM day_summaries").fetchone()["c"],
            "commands": conn.execute("SELECT COUNT(*) c FROM commands").fetchone()["c"],
        }
