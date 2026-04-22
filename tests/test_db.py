"""Tests for db.py — schema init, insert, dedup, status updates."""

import json
from pathlib import Path

import pytest

from job_puller.db import (
    Job,
    connect,
    finish_run,
    get_active_jobs,
    get_job_by_id,
    get_run_stats,
    init_db,
    start_run,
    update_status,
    upsert_job,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def make_job(url: str = "https://example.com/job/1", site: str = "indeed") -> Job:
    return Job(
        url=url,
        title="Senior Product Manager",
        company="Acme Corp",
        location="Remote, US",
        site=site,
        is_remote=True,
        job_type="fulltime",
        description="Great PM role",
        salary_min=150000.0,
        salary_max=200000.0,
        date_posted="2025-01-01",
    )


def test_schema_creates(db_path: Path) -> None:
    with connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"jobs", "runs"} <= tables


def test_start_and_finish_run(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        finish_run(conn, run_id, jobs_fetched=10, jobs_new=5)
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["jobs_fetched"] == 10
    assert row["jobs_new"] == 5


def test_upsert_new_job(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        is_new = upsert_job(conn, make_job(), run_id)
    assert is_new is True


def test_upsert_duplicate_url(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, make_job(), run_id)
        is_new = upsert_job(conn, make_job(), run_id)
    assert is_new is False


def test_upsert_different_urls_both_new(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        a = upsert_job(conn, make_job("https://example.com/1"), run_id)
        b = upsert_job(conn, make_job("https://example.com/2"), run_id)
    assert a is True
    assert b is True


def test_dismissed_excluded_from_active(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, make_job("https://example.com/1"), run_id)
        upsert_job(conn, make_job("https://example.com/2"), run_id)
        job = get_job_by_id(conn, 1)
        update_status(conn, job["id"], "dismissed")
        active = get_active_jobs(conn)
    assert len(active) == 1
    assert active[0]["url"] == "https://example.com/2"


def test_applied_included_in_active(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, make_job(), run_id)
        job = get_job_by_id(conn, 1)
        update_status(conn, job["id"], "applied")
        active = get_active_jobs(conn)
    assert len(active) == 1
    assert active[0]["status"] == "applied"


def test_invalid_status_rejected(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, make_job(), run_id)
        with pytest.raises(Exception):
            update_status(conn, 1, "banana")
            conn.commit()


def test_run_stats(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, make_job("https://example.com/1"), run_id)
        upsert_job(conn, make_job("https://example.com/2"), run_id)
        upsert_job(conn, make_job("https://example.com/3"), run_id)
        update_status(conn, 1, "applied")
        update_status(conn, 2, "dismissed")
        stats = get_run_stats(conn)
    assert stats["total"] == 3
    assert stats["applied"] == 1
    assert stats["dismissed"] == 1
