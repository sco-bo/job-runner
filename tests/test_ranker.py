"""Tests for ranker.py — filtering and sorting."""

from pathlib import Path
import pytest
from job_puller.db import Job, connect, init_db, start_run, update_status, upsert_job
from job_puller.ranker import get_applied_jobs, get_top_jobs


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    init_db(path)
    return path


def _seed(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        for i, score in enumerate([90, 80, 70, 60, 50], start=1):
            upsert_job(conn, Job(url=f"https://example.com/{i}", title=f"PM {i}",
                                 company="Co", location="Remote", site="test", is_remote=True), run_id)
            conn.execute("UPDATE jobs SET score = ? WHERE url = ?", (score, f"https://example.com/{i}"))


def test_top_jobs_sorted_by_score(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        top = get_top_jobs(conn, 20)
    scores = [j.score for j in top]
    assert scores == sorted(scores, reverse=True)


def test_dismissed_excluded(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        update_status(conn, 1, "dismissed")
        top = get_top_jobs(conn, 20)
    assert all(j.id != 1 for j in top)
    assert len(top) == 4


def test_applied_excluded_from_top(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        update_status(conn, 1, "applied")
        top = get_top_jobs(conn, 20)
    assert all(j.id != 1 for j in top)
    assert len(top) == 4


def test_applied_in_applied_list(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        update_status(conn, 1, "applied")
        applied = get_applied_jobs(conn)
    assert len(applied) == 1
    assert applied[0].id == 1


def test_top_n_limit(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        top = get_top_jobs(conn, 3)
    assert len(top) == 3
    assert top[0].score == 90.0


def test_exclude_keywords_filters_titles(db_path: Path) -> None:
    with connect(db_path) as conn:
        run_id = start_run(conn)
        upsert_job(conn, Job(url="https://example.com/mkt", title="Product Marketing Manager",
                             company="Co", location="Remote", site="test", is_remote=True), run_id)
        conn.execute("UPDATE jobs SET score = 85 WHERE url = 'https://example.com/mkt'")
        top = get_top_jobs(conn, 20, exclude_keywords=["product marketing"])
    assert all("product marketing" not in j.title.lower() for j in top)


def test_exclude_keywords_all_returns_without_filter(db_path: Path) -> None:
    _seed(db_path)
    with connect(db_path) as conn:
        top = get_top_jobs(conn, 20, exclude_keywords=[])
    assert len(top) == 5
