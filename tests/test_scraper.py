"""Tests for scraper.py normalization logic (no network calls)."""

from datetime import date, datetime

from job_puller.scraper import _parse_date, _to_float, _is_allowed_location


def test_parse_date_from_date_object():
    assert _parse_date(date(2025, 3, 15)) == "2025-03-15"


def test_parse_date_from_datetime():
    assert _parse_date(datetime(2025, 3, 15, 10, 0)) == "2025-03-15"


def test_parse_date_from_iso_string():
    assert _parse_date("2025-03-15") == "2025-03-15"


def test_parse_date_truncates_datetime_string():
    assert _parse_date("2025-03-15T10:30:00") == "2025-03-15"


def test_parse_date_relative_string_returns_none():
    # JobSpy sometimes returns "3 days ago" — should be skipped
    assert _parse_date("3 days ago") is None


def test_parse_date_none():
    assert _parse_date(None) is None


def test_to_float_int():
    assert _to_float(150000) == 150000.0


def test_to_float_string():
    assert _to_float("200000") == 200000.0


def test_to_float_none():
    assert _to_float(None) is None


def test_to_float_invalid():
    assert _to_float("n/a") is None


# --- Location filter ---
_ALLOWED = ["denver", "boulder", "remote"]

def test_allowed_location_is_remote():
    assert _is_allowed_location("Kyiv, Ukraine", is_remote=True, allowed_locations=_ALLOWED)

def test_allowed_location_denver():
    assert _is_allowed_location("Denver, CO", is_remote=False, allowed_locations=_ALLOWED)

def test_allowed_location_boulder():
    assert _is_allowed_location("Boulder, CO", is_remote=False, allowed_locations=_ALLOWED)

def test_allowed_location_remote_string():
    assert _is_allowed_location("Remote", is_remote=False, allowed_locations=_ALLOWED)

def test_allowed_location_rejects_los_angeles():
    assert not _is_allowed_location("Los Angeles, CA", is_remote=False, allowed_locations=_ALLOWED)

def test_allowed_location_rejects_ukraine():
    assert not _is_allowed_location("Kyiv, Ukraine", is_remote=False, allowed_locations=_ALLOWED)

def test_allowed_location_empty_location():
    assert _is_allowed_location("", is_remote=False, allowed_locations=_ALLOWED)  # unknown — kept
