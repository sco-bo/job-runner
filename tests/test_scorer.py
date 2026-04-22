"""Tests for scorer.py heuristics."""

import pytest
from job_puller.scorer import (
    ScoreResult,
    _location_score,
    _salary_score,
    _seniority_score,
    _skills_score,
    _title_score,
    _tokenize,
    score_job,
)
import sqlite3


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------

def test_tokenize_lowercases():
    assert "python" in _tokenize("Python")


def test_tokenize_removes_stopwords():
    tokens = _tokenize("the product and the team")
    assert "the" not in tokens
    assert "and" not in tokens
    assert "product" in tokens


def test_tokenize_empty():
    assert _tokenize("") == frozenset()


# ---------------------------------------------------------------------------
# _title_score
# ---------------------------------------------------------------------------

def test_title_exact_match():
    pts, note = _title_score("Senior Product Manager", ["Senior Product Manager"])
    assert pts == 30.0


def test_title_partial_match():
    pts, note = _title_score("Product Manager", ["Senior Product Manager"])
    assert 0 < pts < 30


def test_title_no_match():
    pts, note = _title_score("Software Engineer", ["Senior Product Manager"])
    assert pts < 10


def test_title_empty_job():
    pts, _ = _title_score("", ["Senior Product Manager"])
    assert pts == 0.0


# ---------------------------------------------------------------------------
# _skills_score
# ---------------------------------------------------------------------------

_SKILLS_BANK = {
    "bullets": [
        {"id": "a", "text": "Built data pipelines in Snowflake", "themes": ["data", "technical"], "strength": "high"},
        {"id": "b", "text": "Led API integrations", "themes": ["api", "fintech"], "strength": "medium"},
        {"id": "c", "text": "Ran compliance workflows", "themes": ["compliance", "growth"], "strength": "high"},
    ]
}


def test_skills_theme_match():
    pts, note = _skills_score("We need data and compliance experience", _SKILLS_BANK)
    assert pts > 0
    assert "data" in note or "compliance" in note


def test_skills_no_match():
    pts, note = _skills_score("Marketing campaign manager", _SKILLS_BANK)
    assert pts == 0.0


def test_skills_empty_description():
    pts, _ = _skills_score("", _SKILLS_BANK)
    assert pts == 0.0


# ---------------------------------------------------------------------------
# _seniority_score
# ---------------------------------------------------------------------------

def test_seniority_senior_match():
    pts, note = _seniority_score("Senior Product Manager", ["senior", "lead"])
    assert pts == 20.0


def test_seniority_director_match():
    pts, note = _seniority_score("Director of Product", ["director"])
    assert pts == 20.0


def test_seniority_junior_penalized():
    pts, note = _seniority_score("Junior Product Manager", ["senior", "lead"])
    assert pts == 0.0


def test_seniority_unclear_neutral():
    pts, note = _seniority_score("Product Manager", ["senior", "lead"])
    assert pts == 10.0  # neutral half-credit


# ---------------------------------------------------------------------------
# _salary_score
# ---------------------------------------------------------------------------

def test_salary_in_range():
    pts, _ = _salary_score(150000, 200000, 150000, 250000)
    assert pts == 10.0


def test_salary_above_range():
    pts, _ = _salary_score(300000, 400000, 150000, 250000)
    assert pts == 10.0  # above target is not penalized


def test_salary_below_range():
    pts, _ = _salary_score(80000, 100000, 150000, 250000)
    assert pts < 10.0


def test_salary_not_listed():
    pts, note = _salary_score(None, None, 150000, 250000)
    assert pts == 10.0
    assert "neutral" in note


# ---------------------------------------------------------------------------
# _location_score
# ---------------------------------------------------------------------------

def test_location_remote_preferred():
    pts, _ = _location_score(True, "Remote", True, ["Denver, CO"])
    assert pts == 10.0


def test_location_city_match():
    pts, _ = _location_score(False, "Denver, CO, US", False, ["Denver, CO"])
    assert pts == 10.0


def test_location_mismatch():
    pts, _ = _location_score(False, "New York, NY", True, ["Denver, CO"])
    assert pts == 2.0


# ---------------------------------------------------------------------------
# score_job integration (using a mock sqlite3.Row via dict adapter)
# ---------------------------------------------------------------------------

class _FakeRow(dict):
    """sqlite3.Row-like dict for testing."""
    def __getitem__(self, key):
        return super().__getitem__(key)


_PROFILE = {
    "target_titles": ["Senior Product Manager", "Director of Product"],
    "target_levels": ["senior", "director"],
    "target_salary_min": 150000,
    "target_salary_max": 280000,
    "preferred_remote": True,
    "preferred_locations": ["Denver, CO"],
    "avoid_keywords": ["must be on-site"],
}


def _make_row(**kwargs) -> _FakeRow:
    defaults = dict(
        title="Senior Product Manager",
        description="We need data and compliance experience in a senior PM role.",
        is_remote=True,
        location="Remote",
        salary_min=160000.0,
        salary_max=220000.0,
    )
    defaults.update(kwargs)
    return _FakeRow(defaults)


def test_score_job_high_match():
    result = score_job(_make_row(), _PROFILE, _SKILLS_BANK)
    assert isinstance(result, ScoreResult)
    assert result.score >= 60


def test_score_job_avoid_keyword_penalty():
    row = _make_row(description="This role must be on-site in Chicago.")
    result = score_job(row, _PROFILE, _SKILLS_BANK)
    result_clean = score_job(_make_row(), _PROFILE, _SKILLS_BANK)
    assert result.score < result_clean.score * 0.6


def test_score_job_capped_at_100():
    result = score_job(_make_row(), _PROFILE, _SKILLS_BANK)
    assert result.score <= 100.0


def test_score_job_rationale_non_empty():
    result = score_job(_make_row(), _PROFILE, _SKILLS_BANK)
    assert len(result.rationale) > 0
