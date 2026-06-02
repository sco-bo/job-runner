"""Detect state-restricted remote roles from description text."""

import re
import sqlite3

_PATTERNS = [
    r"remote\b.{0,80}\bfollowing states",
    r"remote\b.{0,60}\bstates\s+only",
    r"must\s+reside\s+in",
    r"must\s+sit\s+in\s+(these\s+)?states",
    r"remote\s+eligible\s+in\b",
    r"remote\s+work\s+is\s+available\s+in",
    r"work\s+remotely\s+from.{0,40}\bstates",
    r"restricted\s+to\s+the\s+following\s+states",
    r"only\s+available\s+in\s+the\s+following\s+states",
    r"remote\s+opportunities\s+in\s+the\s+following",
]
_COMPILED = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _PATTERNS]


def is_state_restricted(description: str) -> bool:
    """Return True if the description indicates remote work is limited to specific states."""
    if not description:
        return False
    return any(p.search(description) for p in _COMPILED)


def _colorado_included(description: str, match_start: int) -> bool:
    """Return True if CO or Colorado appears in the state list after the restriction trigger."""
    window = description[match_start:match_start + 600]
    return bool(re.search(r'\bCO\b|\bColorado\b', window))


def flag_all(conn: sqlite3.Connection) -> int:
    """Scan all jobs with descriptions and set state_restricted=1 where detected.

    If the state list does not include CO, also auto-dismisses the job (status = 'dismissed').
    Only processes jobs currently flagged 0 (safe to re-run; won't unflag jobs).
    Returns the count of newly flagged jobs.
    """
    rows = conn.execute(
        "SELECT id, description FROM jobs WHERE description IS NOT NULL AND state_restricted = 0"
    ).fetchall()
    count = 0
    for row in rows:
        for pattern in _COMPILED:
            m = pattern.search(row["description"])
            if m:
                conn.execute("UPDATE jobs SET state_restricted = 1 WHERE id = ?", (row["id"],))
                if not _colorado_included(row["description"], m.start()):
                    conn.execute(
                        "UPDATE jobs SET status = 'dismissed' WHERE id = ? AND status IS NULL",
                        (row["id"],),
                    )
                count += 1
                break
    return count
