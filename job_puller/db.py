"""SQLite interface — schema creation, job insertion with dedup, run tracking."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator, Optional


@dataclass
class Job:
    url: str
    title: str
    company: str
    location: str
    site: str
    is_remote: bool = False
    job_type: Optional[str] = None
    description: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    date_posted: Optional[str] = None
    # Populated by scorer/matcher in later phases
    score: Optional[float] = None
    score_rationale: Optional[str] = None
    recommended_summary: Optional[str] = None
    matched_bullet_ids: list[str] = field(default_factory=list)
    status: Optional[str] = None  # None | "applied" | "dismissed"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at      TEXT    NOT NULL,
    jobs_fetched INTEGER NOT NULL DEFAULT 0,
    jobs_new    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT    NOT NULL UNIQUE,
    title               TEXT,
    company             TEXT,
    location            TEXT,
    is_remote           BOOLEAN NOT NULL DEFAULT 0,
    job_type            TEXT,
    description         TEXT,
    salary_min          REAL,
    salary_max          REAL,
    date_posted         TEXT,
    site                TEXT,
    first_seen_run      INTEGER REFERENCES runs(id),
    score               REAL,
    score_rationale     TEXT,
    recommended_summary TEXT,
    matched_bullet_ids  TEXT,
    status              TEXT    CHECK(status IN ('applied', 'dismissed')) DEFAULT NULL,
    industry            TEXT,
    job_search_id       TEXT,
    description_highlights TEXT,
    state_restricted    INTEGER NOT NULL DEFAULT 0,
    connection_count    INTEGER NOT NULL DEFAULT 0,
    is_saved            INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS connections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    company     TEXT NOT NULL,
    position    TEXT,
    source      TEXT NOT NULL DEFAULT 'manual',
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS onboarding_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    step        INTEGER NOT NULL DEFAULT 0,
    profile     TEXT,
    themes      TEXT,
    parsed_data TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def get_db_path(data_path: str) -> Path:
    path = Path(data_path).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path / "jobs.db"


@contextmanager
def connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        # Migrations for existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "industry" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN industry TEXT")
        if "job_search_id" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN job_search_id TEXT")
        if "description_highlights" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN description_highlights TEXT")
        if "state_restricted" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN state_restricted INTEGER NOT NULL DEFAULT 0")
        if "connection_count" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN connection_count INTEGER NOT NULL DEFAULT 0")
        if "is_saved" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN is_saved INTEGER NOT NULL DEFAULT 0")
        if "applied_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN applied_at TEXT")
        if "created_at" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN created_at TEXT")
            conn.execute("UPDATE jobs SET created_at = date_posted || 'T00:00:00' WHERE created_at IS NULL AND date_posted IS NOT NULL")
            conn.execute("UPDATE jobs SET created_at = datetime('now') WHERE created_at IS NULL")
        # connections table (CREATE IF NOT EXISTS handles new DBs; existing DBs get it here)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                company     TEXT NOT NULL,
                position    TEXT,
                source      TEXT NOT NULL DEFAULT 'manual',
                created_at  TEXT NOT NULL
            )
        """)


def start_run(conn: sqlite3.Connection) -> int:
    from datetime import datetime, timezone
    cur = conn.execute(
        "INSERT INTO runs (run_at) VALUES (?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, jobs_fetched: int, jobs_new: int) -> None:
    conn.execute(
        "UPDATE runs SET jobs_fetched = ?, jobs_new = ? WHERE id = ?",
        (jobs_fetched, jobs_new, run_id),
    )


def upsert_job(conn: sqlite3.Connection, job: Job, run_id: Optional[int] = None) -> bool:
    """Insert job if URL not seen before. Returns True if it was new."""
    existing = conn.execute("SELECT id FROM jobs WHERE url = ?", (job.url,)).fetchone()
    if existing:
        return False

    from datetime import datetime, timezone

    conn.execute(
        """
        INSERT INTO jobs (
            url, title, company, location, is_remote, job_type, description,
            salary_min, salary_max, date_posted, site, first_seen_run,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job.url, job.title, job.company, job.location, job.is_remote,
            job.job_type, job.description, job.salary_min, job.salary_max,
            job.date_posted, job.site, run_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return True


def update_scores(conn: sqlite3.Connection, job_id: int, score: float, rationale: str,
                  recommended_summary: str, matched_bullet_ids: list[str],
                  description_highlights: Optional[list[str]] = None) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET score = ?, score_rationale = ?, recommended_summary = ?, matched_bullet_ids = ?,
            description_highlights = ?
        WHERE id = ?
        """,
        (score, rationale, recommended_summary, json.dumps(matched_bullet_ids),
         json.dumps(description_highlights or []), job_id),
    )


def update_status(conn: sqlite3.Connection, job_id: int, status: Optional[str]) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))


def update_job_search_id(conn: sqlite3.Connection, job_id: int, job_search_id: Optional[str]) -> None:
    conn.execute("UPDATE jobs SET job_search_id = ? WHERE id = ?", (job_search_id, job_id))


def get_job_by_id(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_url(conn: sqlite3.Connection, url: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()


def get_active_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All jobs not dismissed, ordered by score desc (nulls last)."""
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE status IS NULL OR status != 'dismissed'
        ORDER BY score DESC NULLS LAST
        """
    ).fetchall()


def get_dismissed_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All dismissed jobs, ordered by score desc (nulls last)."""
    return conn.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'dismissed'
        ORDER BY score DESC NULLS LAST
        """
    ).fetchall()


def get_runs(conn: sqlite3.Connection) -> list[dict]:
    """Return all runs ordered newest first with smart relative labels.

    Same-day runs include a time suffix (e.g. "Today 9:03am") to distinguish them.
    Recent runs use relative labels ("Today", "Yesterday"); older runs use "Apr 15".
    """
    from datetime import datetime, timedelta, timezone

    rows = conn.execute(
        "SELECT id, run_at, jobs_fetched, jobs_new FROM runs ORDER BY id DESC"
    ).fetchall()

    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    # Count how many runs share each calendar date (to know when time is needed)
    date_counts: dict[str, int] = {}
    for r in rows:
        d = (r["run_at"] or "")[:10]
        date_counts[d] = date_counts.get(d, 0) + 1

    result = []
    for r in rows:
        run_at = r["run_at"] or ""
        date_part = run_at[:10]
        needs_time = date_counts.get(date_part, 0) > 1

        try:
            dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
            full_date = dt.strftime("%B %-d, %Y")
        except ValueError:
            full_date = date_part

        if date_part == today_str:
            base = f"Today ({full_date})"
        elif date_part == yesterday_str:
            base = f"Yesterday ({full_date})"
        else:
            base = full_date

        if needs_time:
            try:
                dt = datetime.fromisoformat(run_at.replace("Z", "+00:00"))
                local_dt = dt.astimezone()
                time_str = local_dt.strftime("%-I:%M%p").lower()
                label = f"{base} {time_str}"
            except ValueError:
                label = base
        else:
            label = base

        result.append({
            "id": r["id"],
            "date_label": label,
            "jobs_fetched": r["jobs_fetched"],
            "jobs_new": r["jobs_new"],
        })
    return result


def add_connection(conn: sqlite3.Connection, name: str, company: str,
                   position: Optional[str] = None, source: str = "manual") -> int:
    from datetime import datetime, timezone
    cur = conn.execute(
        "INSERT INTO connections (name, company, position, source, created_at) VALUES (?, ?, ?, ?, ?)",
        (name, company, position, source, datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid


def delete_connection(conn: sqlite3.Connection, connection_id: int) -> None:
    conn.execute("DELETE FROM connections WHERE id = ?", (connection_id,))


def get_connections(conn: sqlite3.Connection) -> list:
    return conn.execute("SELECT * FROM connections ORDER BY company, name").fetchall()


def toggle_saved(conn: sqlite3.Connection, job_id: int) -> bool:
    """Flip is_saved for a job. Returns the new saved state."""
    new_val = conn.execute(
        "UPDATE jobs SET is_saved = CASE WHEN is_saved = 1 THEN 0 ELSE 1 END WHERE id = ? RETURNING is_saved",
        (job_id,)
    ).fetchone()[0]
    return bool(new_val)


def get_saved_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All saved jobs, ordered by score desc."""
    return conn.execute(
        "SELECT * FROM jobs WHERE is_saved = 1 ORDER BY score DESC NULLS LAST"
    ).fetchall()


def update_connection_count(conn: sqlite3.Connection, job_id: int, count: int) -> None:
    conn.execute("UPDATE jobs SET connection_count = ? WHERE id = ?", (count, job_id))


def get_run_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'applied'   THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN status = 'dismissed' THEN 1 ELSE 0 END) AS dismissed,
            SUM(CASE WHEN date_posted >= date('now', '-1 day') THEN 1 ELSE 0 END) AS new_today
        FROM jobs
        """
    ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Onboarding state
# ---------------------------------------------------------------------------


def init_onboarding(conn: sqlite3.Connection) -> None:
    """Ensure the singleton onboarding_state row exists."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT OR IGNORE INTO onboarding_state (id, step, profile, themes, parsed_data, created_at, updated_at)
        VALUES (1, 0, NULL, NULL, NULL, ?, ?)
        """,
        (now, now),
    )


def get_onboarding_step(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT step FROM onboarding_state WHERE id = 1").fetchone()
    return row["step"] if row else 0


def get_onboarding_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM onboarding_state WHERE id = 1").fetchone()
    if not row:
        return {"step": 0, "profile": None, "themes": None, "parsed_data": None}
    return {
        "step": row["step"],
        "profile": json.loads(row["profile"]) if row["profile"] else None,
        "themes": json.loads(row["themes"]) if row["themes"] else None,
        "parsed_data": json.loads(row["parsed_data"]) if row["parsed_data"] else None,
    }


def save_onboarding_state(
    conn: sqlite3.Connection,
    step: int,
    profile: Optional[dict] = None,
    themes: Optional[list] = None,
    parsed_data: Optional[dict] = None,
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO onboarding_state (id, step, profile, themes, parsed_data, created_at, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            step = excluded.step,
            profile = COALESCE(excluded.profile, onboarding_state.profile),
            themes = COALESCE(excluded.themes, onboarding_state.themes),
            parsed_data = COALESCE(excluded.parsed_data, onboarding_state.parsed_data),
            updated_at = excluded.updated_at
        """,
        (
            step,
            json.dumps(profile) if profile else None,
            json.dumps(themes) if themes else None,
            json.dumps(parsed_data) if parsed_data else None,
            now,
            now,
        ),
    )


def clear_onboarding_state(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM onboarding_state WHERE id = 1")
    init_onboarding(conn)
