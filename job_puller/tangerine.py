"""Tangerine Feed scraper — parses JSON-LD ItemList, fetches details for jobs ≤24h old."""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from job_puller.db import Job

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; job-puller/0.1; "
        "+https://github.com/your-repo/job-puller)"
    )
}


def _parse_date(date_str: str) -> Optional[datetime]:
    """Parse ISO 8601 date or datetime string to UTC datetime."""
    if not date_str:
        return None
    try:
        # Handle both "2025-01-15" and "2025-01-15T10:30:00Z" formats
        s = date_str.strip().rstrip("Z")
        if "T" in s:
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_jsonld(soup: BeautifulSoup, type_name: str) -> Optional[dict]:
    """Find the first JSON-LD block matching @type == type_name."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                for item in data:
                    if item.get("@type") == type_name:
                        return item
            elif data.get("@type") == type_name:
                return data
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def _fetch_page(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    try:
        resp = session.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _parse_salary(soup: BeautifulSoup) -> tuple[Optional[float], Optional[float]]:
    """Extract salary range from a detail page. Returns (min, max) or (None, None)."""
    # Look for structured salary in JSON-LD JobPosting
    jobld = _extract_jsonld(soup, "JobPosting")
    if jobld:
        base = jobld.get("baseSalary", {})
        value = base.get("value", {}) if isinstance(base, dict) else {}
        if isinstance(value, dict):
            min_val = value.get("minValue")
            max_val = value.get("maxValue")
            try:
                return (float(min_val) if min_val else None,
                        float(max_val) if max_val else None)
            except (TypeError, ValueError):
                pass
    # Fallback: scan for a salary pattern in visible text
    text = soup.get_text(" ", strip=True)
    match = re.search(r"\$([0-9,]+)\s*[–\-]\s*\$([0-9,]+)", text)
    if match:
        try:
            lo = float(match.group(1).replace(",", ""))
            hi = float(match.group(2).replace(",", ""))
            return lo, hi
        except ValueError:
            pass
    return None, None


def _find_source_url(soup: BeautifulSoup, tangerine_url: str) -> Optional[str]:
    """Find the original job posting URL from a Tangerine detail page.

    Looks for apply-type links pointing to an external domain.
    Returns the source URL, or None if not found.
    """
    from urllib.parse import urlparse

    tangerine_domain = urlparse(tangerine_url).netloc

    apply_patterns = re.compile(r"\bapply\b", re.IGNORECASE)

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(strip=True)
        if not href.startswith("http"):
            continue
        if urlparse(href).netloc == tangerine_domain:
            continue
        if apply_patterns.search(text) or apply_patterns.search(href):
            return href

    return None


def _parse_description(soup: BeautifulSoup) -> Optional[str]:
    """Extract job description text from a detail page."""
    jobld = _extract_jsonld(soup, "JobPosting")
    if jobld and jobld.get("description"):
        # Strip HTML tags from embedded description
        desc_soup = BeautifulSoup(jobld["description"], "html.parser")
        return desc_soup.get_text("\n", strip=True)
    # Fallback: grab the main content area
    for selector in ["article", "main", "[class*='description']", "[class*='content']"]:
        el = soup.select_one(selector)
        if el:
            return el.get_text("\n", strip=True)
    return None


def scrape(config: dict) -> list[Job]:
    """Scrape Tangerine Feed and return Job objects for postings within hours_old."""
    tcfg = config.get("tangerine", {})
    if not tcfg.get("enabled", True):
        return []

    base_url = tcfg.get("base_url", "")
    if not base_url:
        return []
    sort = tcfg.get("sort", "recent")
    hours_old = int(tcfg.get("hours_old", 24))
    fetch_details = tcfg.get("fetch_details", True)
    delay = float(tcfg.get("delay_seconds", 1.0))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    session = requests.Session()
    jobs: list[Job] = []
    page = 1

    while True:
        url = f"{base_url}?page={page}&sort={sort}"
        logger.info("Tangerine: fetching listing page %d", page)
        soup = _fetch_page(url, session)
        if soup is None:
            break

        item_list = _extract_jsonld(soup, "ItemList")
        if not item_list:
            logger.warning("Tangerine: no ItemList JSON-LD on page %d", page)
            break

        items = item_list.get("itemListElement", [])
        if not items:
            break

        page_had_recent = False

        for list_item in items:
            # Tangerine wraps each entry as {"@type": "ListItem", "item": {JobPosting}}
            item = list_item.get("item", list_item)

            date_posted_str = item.get("datePosted", "")
            posted_dt = _parse_date(date_posted_str)

            if posted_dt and posted_dt < cutoff:
                continue

            page_had_recent = True
            job_url = item.get("url", "").strip()
            if not job_url:
                continue

            title = item.get("title", "").strip()
            company = (item.get("hiringOrganization") or {}).get("name", "").strip()
            date_str = posted_dt.strftime("%Y-%m-%d") if posted_dt else None

            salary_min = salary_max = None
            description = None

            if fetch_details:
                time.sleep(delay)
                logger.debug("Tangerine: fetching detail %s", job_url)
                detail_soup = _fetch_page(job_url, session)
                if detail_soup:
                    salary_min, salary_max = _parse_salary(detail_soup)
                    # Try to follow the apply link to the original job posting
                    source_url = _find_source_url(detail_soup, job_url)
                    if source_url:
                        logger.debug("Tangerine: following source URL %s", source_url)
                        time.sleep(delay)
                        source_soup = _fetch_page(source_url, session)
                        if source_soup:
                            source_desc = _parse_description(source_soup)
                            if source_desc and len(source_desc) > len(_parse_description(detail_soup) or ""):
                                description = source_desc
                                logger.debug("Tangerine: using source description (%d chars)", len(description))
                            else:
                                description = _parse_description(detail_soup)
                        else:
                            description = _parse_description(detail_soup)
                    else:
                        description = _parse_description(detail_soup)

            jobs.append(Job(
                url=job_url,
                title=title,
                company=company,
                location="Remote, US",
                site="tangerine",
                is_remote=True,
                job_type="fulltime",
                description=description,
                salary_min=salary_min,
                salary_max=salary_max,
                date_posted=date_str,
            ))

        if not page_had_recent:
            logger.info("Tangerine: page %d has no recent jobs — stopping", page)
            break

        # Check if there's a next page
        next_link = soup.select_one(f'a[href*="page={page + 1}"]')
        if not next_link:
            break

        page += 1
        time.sleep(delay)

    logger.info("Tangerine returned %d jobs", len(jobs))
    return jobs
