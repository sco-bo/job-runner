"""Watchlist scraper — checks Lever/Ashby job boards for connection companies."""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from job_puller.db import Job

logger = logging.getLogger(__name__)

_CACHE_FILE = Path("~/.job_puller/watchlist_cache.json").expanduser()
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; job-puller/0.1; "
        "+https://github.com/your-repo/job-puller)"
    )
}


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if _CACHE_FILE.exists():
        try:
            return json.loads(_CACHE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_FILE.write_text(json.dumps(cache, indent=2))


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def _to_slugs(name: str) -> list[str]:
    name = re.sub(r"\b(Inc\.?|LLC\.?|Corp\.?|Ltd\.?|Co\.?|the)\b", "", name, flags=re.I)
    name = name.lower().strip()
    no_spaces    = re.sub(r"[^a-z0-9]", "", name)
    with_hyphens = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    seen: dict[str, None] = {}
    for s in [no_spaces, with_hyphens]:
        if s:
            seen[s] = None
    return list(seen)


# ---------------------------------------------------------------------------
# ATS probing
# ---------------------------------------------------------------------------

def _probe_lever(slug: str, session: requests.Session) -> bool:
    try:
        r = session.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            headers=_HEADERS, timeout=8,
        )
        return r.status_code == 200 and bool(r.json())
    except Exception:
        return False


def _probe_ashby(slug: str, session: requests.Session) -> bool:
    try:
        r = session.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            headers=_HEADERS, timeout=8,
        )
        if r.status_code != 200:
            return False
        data = r.json()
        return "jobPostings" in data
    except Exception:
        return False


def _discover(company: str, cache: dict, null_days: int, session: requests.Session, delay: float) -> Optional[tuple[str, str]]:
    """Return (ats, slug) if company uses Lever or Ashby, else None.

    Uses cache; re-probes null entries only after null_days.
    """
    key = company.lower().strip()
    entry = cache.get(key)
    now = datetime.now(timezone.utc).isoformat()

    if entry:
        if entry.get("ats"):
            return entry["ats"], entry["slug"]
        # null entry — skip if checked recently
        checked = entry.get("checked_at", "")
        if checked:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(checked)
                if age.days < null_days:
                    return None
            except ValueError:
                pass

    slugs = _to_slugs(company)
    for slug in slugs:
        time.sleep(delay)
        if _probe_lever(slug, session):
            cache[key] = {"ats": "lever", "slug": slug, "checked_at": now}
            logger.debug("watchlist: %s → Lever (%s)", company, slug)
            return "lever", slug
        time.sleep(delay)
        if _probe_ashby(slug, session):
            cache[key] = {"ats": "ashby", "slug": slug, "checked_at": now}
            logger.debug("watchlist: %s → Ashby (%s)", company, slug)
            return "ashby", slug

    cache[key] = {"ats": None, "checked_at": now}
    return None


# ---------------------------------------------------------------------------
# Job fetching
# ---------------------------------------------------------------------------

def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)


def _fetch_lever_jobs(slug: str, company: str, cutoff: datetime,
                      keywords: list[str], session: requests.Session) -> list[Job]:
    jobs: list[Job] = []
    try:
        r = session.get(
            f"https://api.lever.co/v0/postings/{slug}?mode=json",
            headers=_HEADERS, timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        logger.warning("watchlist: lever fetch failed for %s: %s", slug, exc)
        return jobs

    for p in r.json():
        title = p.get("text", "").strip()
        if not _title_matches(title, keywords):
            continue

        created_ms = p.get("createdAt", 0)
        if created_ms:
            posted_dt = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc)
            if posted_dt < cutoff:
                continue
            date_str = posted_dt.strftime("%Y-%m-%d")
        else:
            date_str = None

        url = p.get("hostedUrl", "").strip()
        if not url:
            continue

        cats = p.get("categories", {})
        location = cats.get("location", "") or cats.get("allLocations", [""])[0] if cats.get("allLocations") else ""
        is_remote = "remote" in location.lower() or "remote" in title.lower()

        description = _strip_html(p.get("description", "") or p.get("descriptionPlain", ""))

        salary_range = p.get("salaryRange") or {}
        salary_min = float(salary_range["min"]) if salary_range.get("min") else None
        salary_max = float(salary_range["max"]) if salary_range.get("max") else None

        jobs.append(Job(
            url=url,
            title=title,
            company=company,
            location=location or "Remote",
            site="watchlist",
            is_remote=is_remote,
            job_type="fulltime",
            description=description or None,
            salary_min=salary_min,
            salary_max=salary_max,
            date_posted=date_str,
        ))

    return jobs


def _fetch_ashby_jobs(slug: str, company: str, cutoff: datetime,
                      keywords: list[str], session: requests.Session) -> list[Job]:
    jobs: list[Job] = []
    try:
        r = session.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}",
            headers=_HEADERS, timeout=15,
        )
        r.raise_for_status()
    except Exception as exc:
        logger.warning("watchlist: ashby fetch failed for %s: %s", slug, exc)
        return jobs

    for p in r.json().get("jobPostings", []):
        title = p.get("title", "").strip()
        if not _title_matches(title, keywords):
            continue

        published = p.get("publishedDate", "")
        if published:
            try:
                posted_dt = datetime.fromisoformat(published.rstrip("Z")).replace(tzinfo=timezone.utc)
                if posted_dt < cutoff:
                    continue
                date_str = posted_dt.strftime("%Y-%m-%d")
            except ValueError:
                date_str = None
        else:
            date_str = None

        url = p.get("jobUrl", "").strip()
        if not url:
            continue

        loc_data = p.get("location") or {}
        location = loc_data.get("name", "") if isinstance(loc_data, dict) else str(loc_data)
        is_remote = p.get("isRemote", False) or "remote" in location.lower()

        description = p.get("descriptionPlainText") or _strip_html(p.get("descriptionHtml", ""))

        salary_min = salary_max = None
        for comp in (p.get("compensation") or {}).get("summaryComponents", []):
            lo = comp.get("minValue")
            hi = comp.get("maxValue")
            if lo:
                salary_min = float(lo)
            if hi:
                salary_max = float(hi)

        jobs.append(Job(
            url=url,
            title=title,
            company=company,
            location=location or "Remote",
            site="watchlist",
            is_remote=is_remote,
            job_type="fulltime",
            description=description or None,
            salary_min=salary_min,
            salary_max=salary_max,
            date_posted=date_str,
        ))

    return jobs


# ---------------------------------------------------------------------------
# Title keyword filter
# ---------------------------------------------------------------------------

def _title_matches(title: str, keywords: list[str]) -> bool:
    t = title.lower()
    return any(kw.lower() in t for kw in keywords)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape(config: dict) -> list[Job]:
    """Probe connection companies against Lever/Ashby and return matching PM jobs."""
    cfg = config.get("watchlist", {})
    if not cfg.get("enabled", True):
        return []

    hours_old  = int(cfg.get("hours_old", 24))
    delay      = float(cfg.get("delay_seconds", 0.5))
    null_days  = int(cfg.get("cache_null_days", 30))
    keywords: list[str] = cfg.get("title_keywords", [
        "product manager", "product owner",
        "group product manager", "principal product manager",
    ])

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_old)

    db_path = Path(config.get("data_path", "~/.job_puller")).expanduser() / "jobs.db"
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT DISTINCT company FROM connections WHERE company != ''").fetchall()
        companies = [r["company"] for r in rows]
        conn.close()
    except Exception as exc:
        logger.warning("watchlist: could not read connections: %s", exc)
        return []

    if not companies:
        logger.info("watchlist: no connections found, skipping")
        return []

    cache = _load_cache()
    session = requests.Session()
    all_jobs: list[Job] = []

    lever_count = ashby_count = probed = 0

    for company in companies:
        result = _discover(company, cache, null_days, session, delay)
        if result is None:
            continue
        ats, slug = result
        probed += 1

        if ats == "lever":
            lever_count += 1
            jobs = _fetch_lever_jobs(slug, company, cutoff, keywords, session)
        else:
            ashby_count += 1
            jobs = _fetch_ashby_jobs(slug, company, cutoff, keywords, session)

        all_jobs.extend(jobs)
        if jobs:
            logger.info("watchlist: %s (%s) → %d job(s)", company, ats, len(jobs))
        time.sleep(delay)

    _save_cache(cache)

    logger.info(
        "watchlist: probed %d companies (%d Lever, %d Ashby), %d jobs found",
        probed, lever_count, ashby_count, len(all_jobs),
    )
    return all_jobs
