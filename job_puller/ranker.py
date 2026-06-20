"""Ranker — filters and sorts scored jobs, returns top N for the digest."""

import sqlite3
from dataclasses import dataclass
from typing import Optional


@dataclass
class RankedJob:
    id: int
    title: str
    company: str
    location: str
    is_remote: bool
    site: str
    url: str
    score: float
    score_rationale: str
    salary_min: Optional[float]
    salary_max: Optional[float]
    date_posted: Optional[str]
    status: Optional[str]
    recommended_summary: Optional[str]
    matched_bullet_ids: list[str]
    industry: Optional[str]
    first_seen_run: Optional[int]
    created_at: Optional[str]
    description_highlights: list[str]
    state_restricted: bool = False
    connection_count: int = 0
    is_saved: bool = False


def _row_to_ranked(row: sqlite3.Row) -> RankedJob:
    import json
    raw_ids = row["matched_bullet_ids"]
    bullet_ids = json.loads(raw_ids) if raw_ids else []
    return RankedJob(
        id=row["id"],
        title=row["title"] or "",
        company=row["company"] or "",
        location=row["location"] or "",
        is_remote=bool(row["is_remote"]),
        site=row["site"] or "",
        url=row["url"] or "",
        score=row["score"] or 0.0,
        score_rationale=row["score_rationale"] or "",
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        date_posted=row["date_posted"],
        status=row["status"],
        recommended_summary=row["recommended_summary"],
        matched_bullet_ids=bullet_ids,
        industry=row["industry"] if "industry" in row.keys() else None,
        first_seen_run=row["first_seen_run"] if "first_seen_run" in row.keys() else None,
        created_at=row["created_at"] if "created_at" in row.keys() else None,
        description_highlights=json.loads(row["description_highlights"]) if row["description_highlights"] else [],
        state_restricted=bool(row["state_restricted"]) if "state_restricted" in row.keys() else False,
        connection_count=row["connection_count"] if "connection_count" in row.keys() else 0,
        is_saved=bool(row["is_saved"]) if "is_saved" in row.keys() else False,
    )


def _excluded(title: str, exclude_keywords: list[str]) -> bool:
    """Return True if title contains any exclude keyword (case-insensitive)."""
    t = title.lower()
    return any(kw.lower() in t for kw in exclude_keywords)


def get_top_jobs(
    conn: sqlite3.Connection,
    top_n: int = 20,
    exclude_keywords: Optional[list[str]] = None,
    run_id: Optional[int] = None,
    per_source_min: int = 5,
) -> list[RankedJob]:
    """Return top N scored jobs, excluding dismissed, applied, and title-excluded roles.

    After the global top-N is assembled, any source with zero representation gets
    its top `per_source_min` jobs appended so every source is always visible.
    Pass run_id to restrict to jobs first seen in that run.
    """
    run_filter = "AND first_seen_run = ?" if run_id is not None else ""
    run_params: tuple = (run_id,) if run_id is not None else ()

    fetch_n = top_n * 4 if exclude_keywords else top_n * 2
    rows = conn.execute(
        f"""
        SELECT * FROM jobs
        WHERE score IS NOT NULL
          AND status IS NULL
          {run_filter}
        ORDER BY score DESC
        LIMIT ?
        """,
        (*run_params, fetch_n),
    ).fetchall()

    results: list[RankedJob] = []
    for r in rows:
        job = _row_to_ranked(r)
        if exclude_keywords and _excluded(job.title, exclude_keywords):
            continue
        results.append(job)
        if len(results) >= top_n:
            break

    # Ensure every source that exists in the DB is represented
    seen_ids = {j.id for j in results}
    seen_sources = {j.site for j in results}

    all_sources = {
        row[0] for row in
        conn.execute("SELECT DISTINCT site FROM jobs WHERE score IS NOT NULL AND status IS NULL").fetchall()
        if row[0]
    }

    for source in sorted(all_sources - seen_sources):
        top_source_rows = conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE score IS NOT NULL
              AND status IS NULL
              AND site = ?
              {run_filter}
            ORDER BY score DESC
            LIMIT ?
            """,
            (source, *run_params, per_source_min),
        ).fetchall()
        for r in top_source_rows:
            job = _row_to_ranked(r)
            if job.id in seen_ids:
                continue
            if exclude_keywords and _excluded(job.title, exclude_keywords):
                continue
            results.append(job)
            seen_ids.add(job.id)

    return results


def get_manual_jobs(conn: sqlite3.Connection) -> list[RankedJob]:
    """Return all manually-added active jobs, ordered by insertion order (newest first)."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE site = 'manual' AND status IS NULL ORDER BY id DESC"
    ).fetchall()
    return [_row_to_ranked(r) for r in rows]


def get_saved_jobs(conn: sqlite3.Connection) -> list[RankedJob]:
    """Return all saved jobs, ordered by run date desc then score desc."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE is_saved = 1 ORDER BY first_seen_run DESC NULLS LAST, score DESC NULLS LAST"
    ).fetchall()
    return [_row_to_ranked(r) for r in rows]


def get_applied_jobs(conn: sqlite3.Connection) -> list[RankedJob]:
    """Return all applied jobs, ordered by date applied desc (newest first)."""
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE status = 'applied'
        ORDER BY applied_at DESC NULLS LAST, first_seen_run DESC NULLS LAST
        """
    ).fetchall()
    return [_row_to_ranked(r) for r in rows]
