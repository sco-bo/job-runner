"""Product Manager Job Board scraper — scrapes productmanagerjobboard.com."""

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

from job_puller.db import Job
from job_puller.scraper import _extract_salary_from_text, _has_non_us_signals

logger = logging.getLogger(__name__)

_BASE = "https://www.productmanagerjobboard.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; job-puller/0.1; "
        "+https://github.com/your-repo/job-puller)"
    )
}

# Span texts that are structural noise, not field data
_SKIP_SPANS = {"featured", "salary transparent", "hiring urgently"}
_JOB_TYPES = {"full-time", "part-time", "contract", "internship", "freelance"}
_SENIORITY = {"entry", "mid", "senior", "lead", "director", "principal", "vp", "c-level"}


def _fetch_page(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    try:
        resp = session.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logger.warning("pmjb: failed to fetch %s: %s", url, exc)
        return None


def _parse_relative_date(text: str) -> Optional[datetime]:
    """Convert '3 weeks ago', '15h ago', '2 days ago' → UTC datetime."""
    now = datetime.now(timezone.utc)
    t = text.lower().strip()
    if "just now" in t or t == "today":
        return now
    patterns = [
        (r"(\d+)\s*h(?:ours?)?\s*ago", "hours"),
        (r"(\d+)\s*days?\s*ago", "days"),
        (r"(\d+)\s*weeks?\s*ago", "weeks"),
        (r"(\d+)\s*months?\s*ago", "months"),
    ]
    for pattern, unit in patterns:
        m = re.search(pattern, t)
        if m:
            n = int(m.group(1))
            if unit == "hours":
                return now - timedelta(hours=n)
            if unit == "days":
                return now - timedelta(days=n)
            if unit == "weeks":
                return now - timedelta(weeks=n)
            if unit == "months":
                return now - timedelta(days=n * 30)
    return None


def _parse_card(card: BeautifulSoup) -> Optional[dict]:
    """Extract fields from a job listing card <a> element."""
    href = card.get("href", "")
    if not href.startswith("/jobs/"):
        return None

    title_el = card.find("h3")
    company_el = card.find("p")
    if not title_el or not company_el:
        return None

    title = title_el.get_text(strip=True)
    company = company_el.get_text(strip=True)

    # Collect unique span texts, filtering structural noise
    seen: set = set()
    spans = []
    for s in card.find_all("span"):
        t = s.get_text(strip=True)
        if not t or t in seen or t.lower() in _SKIP_SPANS or len(t) == 1:
            continue
        seen.add(t)
        spans.append(t)

    location = ""
    job_type = None
    date_text = ""
    salary_text = ""

    for span in spans:
        sl = span.lower()
        if "ago" in sl:
            date_text = span
        elif span.startswith("$") or re.search(r"\$\s*\d", span):
            salary_text = span
        elif sl.replace("-", "").replace(" ", "") in {j.replace("-", "").replace(" ", "") for j in _JOB_TYPES}:
            job_type = sl.replace("-", "").replace(" ", "")
            if job_type == "fulltime":
                job_type = "fulltime"
        elif sl in _SENIORITY:
            pass  # seniority level — not mapped to Job fields
        elif not location:
            location = span

    salary_min, salary_max = _extract_salary_from_text(salary_text or "")
    posted_dt = _parse_relative_date(date_text)

    return {
        "url": _BASE + href,
        "title": title,
        "company": company,
        "location": location,
        "job_type": job_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "posted_dt": posted_dt,
        "date_str": posted_dt.strftime("%Y-%m-%d") if posted_dt else None,
    }


def _fetch_description(url: str, session: requests.Session) -> Optional[str]:
    """Fetch detail page and extract job description text."""
    soup = _fetch_page(url, session)
    if soup is None:
        return None
    main = soup.select_one("main")
    if not main:
        return None

    # Stop before "Similar Jobs" section to avoid pulling in other job titles
    similar = main.find(lambda tag: tag.name in ("h2", "h3") and
                        "similar" in tag.get_text(strip=True).lower())
    if similar:
        for el in similar.find_all_next():
            el.decompose()
        similar.decompose()

    # Grab from "About the Role" onwards if present, else full main text
    about = main.find(lambda tag: tag.name in ("h2", "h3") and
                      "about" in tag.get_text(strip=True).lower())
    if about:
        parts = [about.get_text(strip=True)]
        for sibling in about.find_next_siblings():
            parts.append(sibling.get_text("\n", strip=True))
        return "\n".join(parts)[:5000]

    return main.get_text("\n", strip=True)[:5000]


def scrape(config: dict) -> list[Job]:
    """Scrape productmanagerjobboard.com and return Job objects ≤ hours_old."""
    cfg = config.get("pmjb", {})
    if not cfg.get("enabled", True):
        return []

    hours_old = int(cfg.get("hours_old", 24))
    fetch_descriptions = cfg.get("fetch_descriptions", True)
    delay = float(cfg.get("delay_seconds", 1.0))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    session = requests.Session()
    jobs: list[Job] = []
    page = 1

    while True:
        url = f"{_BASE}/jobs?page={page}"
        logger.info("pmjb: fetching listing page %d", page)
        soup = _fetch_page(url, session)
        if soup is None:
            break

        cards = [a for a in soup.find_all("a", href=True)
                 if a["href"].startswith("/jobs/") and len(a["href"]) > 10]
        if not cards:
            break

        page_had_recent = False

        for card in cards:
            data = _parse_card(card)
            if data is None:
                continue

            posted_dt = data["posted_dt"]
            if posted_dt and posted_dt < cutoff:
                continue
            page_had_recent = True

            location = data["location"]
            is_remote = "remote" in location.lower()

            # Filter non-US roles
            if _has_non_us_signals("", location):
                logger.debug("pmjb: skipping non-US job %r at %r", data["title"], location)
                continue

            description = None
            if fetch_descriptions and data["url"]:
                time.sleep(delay)
                logger.debug("pmjb: fetching detail %s", data["url"])
                description = _fetch_description(data["url"], session)

            jobs.append(Job(
                url=data["url"],
                title=data["title"],
                company=data["company"],
                location=location,
                site="pmjb",
                is_remote=is_remote,
                job_type=data["job_type"],
                description=description,
                salary_min=data["salary_min"],
                salary_max=data["salary_max"],
                date_posted=data["date_str"],
            ))

        if not page_had_recent:
            logger.info("pmjb: page %d has no recent jobs — stopping", page)
            break

        next_link = soup.select_one(f'a[href*="page={page + 1}"]')
        if not next_link:
            break

        page += 1
        time.sleep(delay)

    logger.info("pmjb returned %d jobs", len(jobs))
    return jobs
