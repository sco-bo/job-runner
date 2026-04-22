"""Match network connections against job companies and set connection_count."""

import sqlite3


def match_all(conn: sqlite3.Connection) -> int:
    """Set connection_count on jobs whose company exactly matches a known connection.

    Only processes jobs with connection_count = 0 (won't overwrite manual overrides).
    Case-insensitive exact match on company name.
    Returns count of jobs updated.
    """
    conn_rows = conn.execute("SELECT company FROM connections").fetchall()
    if not conn_rows:
        return 0

    # Build a map: normalized company name → count of connections there
    company_counts: dict[str, int] = {}
    for row in conn_rows:
        key = (row["company"] or "").strip().lower()
        if key:
            company_counts[key] = company_counts.get(key, 0) + 1

    job_rows = conn.execute(
        """
        SELECT id, company FROM jobs
        WHERE connection_count = 0
          AND (status IS NULL OR status != 'dismissed')
        """
    ).fetchall()

    count = 0
    for row in job_rows:
        key = (row["company"] or "").strip().lower()
        n = company_counts.get(key, 0)
        if n > 0:
            conn.execute("UPDATE jobs SET connection_count = ? WHERE id = ?", (n, row["id"]))
            count += 1

    return count
