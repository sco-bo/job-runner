"""Tests for tangerine.py parsing logic (no network calls)."""

import json
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from job_puller.tangerine import _extract_jsonld, _parse_date, _parse_salary


def _make_soup(jsonld: dict) -> BeautifulSoup:
    script = f'<script type="application/ld+json">{json.dumps(jsonld)}</script>'
    return BeautifulSoup(f"<html><head>{script}</head></html>", "html.parser")


def test_parse_date_iso_date():
    dt = _parse_date("2025-03-15")
    assert dt is not None
    assert dt.year == 2025
    assert dt.month == 3
    assert dt.day == 15


def test_parse_date_iso_datetime():
    dt = _parse_date("2025-03-15T10:30:00Z")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_date_empty():
    assert _parse_date("") is None


def test_parse_date_invalid():
    assert _parse_date("not-a-date") is None


def test_extract_jsonld_matches_type():
    soup = _make_soup({"@type": "ItemList", "itemListElement": []})
    result = _extract_jsonld(soup, "ItemList")
    assert result is not None
    assert result["@type"] == "ItemList"


def test_extract_jsonld_no_match():
    soup = _make_soup({"@type": "Organization", "name": "Acme"})
    assert _extract_jsonld(soup, "ItemList") is None


def test_extract_jsonld_list_of_schemas():
    script = json.dumps([
        {"@type": "BreadcrumbList"},
        {"@type": "ItemList", "itemListElement": [{"@type": "ListItem", "item": {"title": "PM Role"}}]},
    ])
    soup = BeautifulSoup(
        f'<html><head><script type="application/ld+json">{script}</script></head></html>',
        "html.parser",
    )
    result = _extract_jsonld(soup, "ItemList")
    assert result is not None
    assert result["itemListElement"][0]["item"]["title"] == "PM Role"


def test_parse_salary_from_jsonld():
    jobld = {
        "@type": "JobPosting",
        "baseSalary": {
            "value": {"@type": "MonetaryAmountDistribution", "minValue": 150000, "maxValue": 200000}
        },
    }
    soup = _make_soup(jobld)
    lo, hi = _parse_salary(soup)
    assert lo == 150000.0
    assert hi == 200000.0


def test_parse_salary_from_text_pattern():
    html = "<html><body>Salary: $150,000 – $200,000 per year</body></html>"
    soup = BeautifulSoup(html, "html.parser")
    lo, hi = _parse_salary(soup)
    assert lo == 150000.0
    assert hi == 200000.0


def test_parse_salary_none_when_missing():
    soup = BeautifulSoup("<html><body>No salary info here</body></html>", "html.parser")
    lo, hi = _parse_salary(soup)
    assert lo is None
    assert hi is None
