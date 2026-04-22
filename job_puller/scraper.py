"""JobSpy wrapper — scrapes LinkedIn/Indeed/Glassdoor and normalizes to Job objects."""

import logging
import re
from datetime import date, datetime
from typing import Any, Optional

from job_puller.db import Job

logger = logging.getLogger(__name__)


def _parse_date(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, (date, datetime)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()[:10]  # truncate to YYYY-MM-DD length
    # Validate it looks like a date (YYYY-MM-DD); jobspy sometimes returns "3 days ago"
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return None
    return s


def _extract_salary_from_text(text: str) -> tuple[Optional[float], Optional[float]]:
    """Parse salary range from free-form description text.

    Handles patterns like:
      $180k-$195k, $180,000-$195,000, $180K to $195K, $180k – $195k
    Returns (min, max) in raw dollars or (None, None) if not found.
    """
    if not text:
        return None, None
    # Match patterns: $NNN[k|K|,000] [–-to] $NNN[k|K|,000]
    pattern = r"\$([0-9]{2,3}(?:[,\.][0-9]{3})?)\s*[kK]?\s*(?:–|-|to)\s*\$([0-9]{2,3}(?:[,\.][0-9]{3})?)\s*[kK]?"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None, None
    try:
        def _parse(s: str, suffix_char: str) -> float:
            s = s.replace(",", "")
            val = float(s)
            if suffix_char.lower() == "k" or val < 1000:
                val *= 1000
            return val

        raw = match.group(0)
        lo_str, hi_str = match.group(1), match.group(2)
        # Detect trailing k
        lo_k = "k" if re.search(r"\$" + re.escape(lo_str) + r"\s*[kK]", raw) else ""
        hi_k = "k" if re.search(re.escape(hi_str) + r"\s*[kK]", raw) else ""
        lo = _parse(lo_str, lo_k)
        hi = _parse(hi_str, hi_k)
        return lo, hi
    except (ValueError, AttributeError):
        return None, None


def _to_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# Keywords that strongly indicate a non-US role when found in title or description.
# Only checked when the location field is blank.
_NON_US_SIGNALS = [
    "united kingdom", " uk ", "(uk)", "uk-", "-uk",
    "england", "scotland", "wales", "northern ireland", "great britain",
    "london", "manchester", "birmingham", "edinburgh", "bristol", "leeds",
    "dublin", "ireland",
    "canada", "toronto", "vancouver", "montreal",
    "australia", "sydney", "melbourne",
    "india", "bangalore", "bengaluru", "mumbai", "hyderabad",
    "germany", "berlin", "munich", "frankfurt",
    "france", "paris",
    "netherlands", "amsterdam",
    "singapore", "hong kong",
    "uae", "united arab emirates", "dubai", "abu dhabi",
    "saudi arabia", "riyadh", "qatar", "doha",
    "israel", "tel aviv",
    "brazil", "são paulo", "mexico", "mexico city",
    "emea", "europe", "apac",
    " £", "£ ", " gbp", "gbp ",
    " aed", " sar", " inr",
]


_REMOTE_ELIGIBLE_SIGNALS = [
    "remote eligible",
    "remote-eligible",
    "work virtually",
    "work from home",
    "work remotely",
    "fully remote",
    "remote work",
    "remote position",
    "remote opportunity",
    "work from anywhere",
    "100% remote",
    "this role is remote",
    "position is remote",
    "role is remote",
]


def _has_non_us_signals(title: str, description: str) -> bool:
    """Return True if title or description contains known non-US location signals."""
    text = f" {title} {description} ".lower()
    return any(signal in text for signal in _NON_US_SIGNALS)


def _has_remote_eligible_signals(description: str) -> bool:
    """Return True if the job description explicitly mentions remote eligibility."""
    text = description.lower()
    return any(signal in text for signal in _REMOTE_ELIGIBLE_SIGNALS)


def _is_allowed_location(location: str, is_remote: bool, allowed_locations: list[str],
                         title: str = "", description: str = "") -> bool:
    """Return True if the job is in an allowed location or is truly remote.

    A job passes if:
      - location contains "remote"
      - location matches an allowed_locations entry (case-insensitive substring)
      - location is blank AND no non-US signals are found in title/description
      - location is a US city (no non-US signals in the location string) AND
        the description explicitly mentions remote eligibility

    is_remote=True from the job board is NOT sufficient on its own — LinkedIn marks
    hybrid roles as remote. We require the location to also be blank, "remote", or
    in the allowed list, or the description must say so explicitly.
    """
    loc = location.strip()

    if not loc:
        return not _has_non_us_signals(title, description)

    loc_lower = loc.lower()
    if "remote" in loc_lower:
        return True
    if any(allowed.lower() in loc_lower for allowed in allowed_locations):
        return True

    # US city with explicit remote-eligible language in the description
    loc_has_non_us = _has_non_us_signals("", loc)
    if not loc_has_non_us and description and _has_remote_eligible_signals(description):
        return True

    return False


def scrape(config: dict) -> list[Job]:
    """Run JobSpy with the given search config and return normalized Job objects."""
    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.error("python-jobspy is not installed. Run: pip install python-jobspy")
        return []

    search_cfg = config.get("search", {})
    sites = search_cfg.get("sites", ["indeed"])
    terms = search_cfg.get("terms", [])
    locations = search_cfg.get("locations", ["Remote"])
    results_per_site = search_cfg.get("results_per_site", 25)
    hours_old = search_cfg.get("hours_old", 24)
    job_type = search_cfg.get("job_type", None)
    is_remote_cfg = search_cfg.get("is_remote", None)
    allowed_locations = search_cfg.get("allowed_locations", ["denver", "boulder", "remote"])
    proxies = config.get("proxies", None)

    jobs: list[Job] = []

    for term in terms:
        for location in locations:
            logger.info("Scraping JobSpy: term=%r location=%r sites=%s", term, location, sites)
            try:
                kwargs: dict = dict(
                    site_name=sites,
                    search_term=term,
                    location=location,
                    results_wanted=results_per_site,
                    hours_old=hours_old,
                    job_type=job_type,
                    linkedin_fetch_description=True,
                    proxies=proxies,
                    verbose=0,
                )
                # Only pass is_remote if explicitly set; newer JobSpy rejects None
                if is_remote_cfg is not None:
                    kwargs["is_remote"] = bool(is_remote_cfg)
                df = scrape_jobs(**kwargs)
            except Exception as exc:
                logger.warning("JobSpy scrape failed for term=%r location=%r: %s", term, location, exc)
                continue

            if df is None or df.empty:
                logger.info("No results for term=%r location=%r", term, location)
                continue

            for _, row in df.iterrows():
                url = str(row.get("job_url") or "").strip()
                if not url:
                    continue
                loc = str(row.get("location") or "").strip()
                remote = bool(row.get("is_remote", False))
                title_str = str(row.get("title") or "").strip()
                description_str = str(row.get("description") or "").strip()
                if not _is_allowed_location(loc, remote, allowed_locations, title_str, description_str):
                    logger.debug("Skipping out-of-area job: %r at %r", row.get("title"), loc)
                    continue
                description = str(row.get("description") or "").strip() or None
                salary_min = _to_float(row.get("min_amount"))
                salary_max = _to_float(row.get("max_amount"))
                # Fall back to parsing salary from description text if JobSpy didn't extract it
                if salary_min is None and salary_max is None and description:
                    salary_min, salary_max = _extract_salary_from_text(description)

                jobs.append(Job(
                    url=url,
                    title=str(row.get("title") or "").strip(),
                    company=str(row.get("company") or "").strip(),
                    location=str(row.get("location") or "").strip(),
                    site=str(row.get("site") or "").strip(),
                    is_remote=bool(row.get("is_remote", False)),
                    job_type=str(row.get("job_type") or "").strip() or None,
                    description=description,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    date_posted=_parse_date(row.get("date_posted")),
                ))

    logger.info("JobSpy returned %d jobs total", len(jobs))
    return jobs
