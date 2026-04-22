"""Detect state-restricted remote roles from description text."""

import re
import sqlite3

_PATTERNS = [
    r"remote\b.{0,80}\bfollowing states",
    r"remote\b.{0,60}\bstates\s+only",
    r"must\s+reside\s+in",
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


def flag_all(conn: sqlite3.Connection) -> int:
    """Scan all jobs with descriptions and set state_restricted=1 where detected.

    Only processes jobs currently flagged 0 (safe to re-run; won't unflag jobs).
    Returns the count of newly flagged jobs.
    """
    rows = conn.execute(
        "SELECT id, description FROM jobs WHERE description IS NOT NULL AND state_restricted = 0"
    ).fetchall()
    count = 0
    for row in rows:
        if is_state_restricted(row["description"]):
            conn.execute("UPDATE jobs SET state_restricted = 1 WHERE id = ?", (row["id"],))
            count += 1
    return count
