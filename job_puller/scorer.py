"""Keyword heuristic scorer — produces a 0-100 score and rationale for each job.

No API calls. No external dependencies beyond PyYAML (loaded by caller).

Scoring weights (must sum to 100):
  Title match      30 pts
  Skills overlap   30 pts
  Seniority match  20 pts
  Salary fit       10 pts
  Location/remote  10 pts

avoid_keywords in profile apply a 0.5x multiplier to the final score.
"""

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Stopwords — excluded from keyword overlap matching
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset(
    "a an the and or but in on at to for of with is are was were be been "
    "have has had do does did will would could should may might shall can "
    "this that these those i we you he she it they our your his her its "
    "their from by as up out if so no not we're you're they're i'm we've "
    "you've they've i've we'll you'll they'll i'll we'd you'd they'd i'd "
    "about into over after before between through during because while when "
    "where who which whom what how all any both each few more most other "
    "some such than then there here also just only very well back".split()
)


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase, strip punctuation, remove stopwords."""
    if not text:
        return frozenset()
    tokens = re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)*", text.lower())
    return frozenset(t for t in tokens if t not in _STOPWORDS and len(t) > 1)


def _title_score(job_title: str, target_titles: list[str]) -> tuple[float, str]:
    """30 pts. Partial credit for partial matches."""
    if not job_title or not target_titles:
        return 0.0, "no title data"

    job_tokens = _tokenize(job_title)
    best = 0.0
    best_match = ""

    for target in target_titles:
        target_tokens = _tokenize(target)
        if not target_tokens:
            continue
        overlap = len(job_tokens & target_tokens) / len(target_tokens)
        if overlap > best:
            best = overlap
            best_match = target

    pts = round(best * 30, 1)
    if pts >= 27:
        rationale = f"strong title match ({best_match!r})"
    elif pts >= 15:
        rationale = f"partial title match ({best_match!r})"
    else:
        rationale = "weak title match"
    return pts, rationale


def _skills_score(description: str, skills_bank: dict) -> tuple[float, str]:
    """30 pts. Matches job description tokens against bullet themes and text."""
    if not description or not skills_bank:
        return 0.0, "no description"

    desc_tokens = _tokenize(description)
    bullets = skills_bank.get("bullets", [])
    if not bullets:
        return 0.0, "no bullets in skills bank"

    # Collect all unique themes present across all bullets
    all_themes: set[str] = set()
    for b in bullets:
        all_themes.update(b.get("themes", []))

    # Count distinct themes whose name appears as a token in the description
    matched_themes = {t for t in all_themes if t.replace("-", "") in desc_tokens
                      or t in desc_tokens
                      or any(part in desc_tokens for part in t.split("-"))}

    theme_ratio = len(matched_themes) / max(len(all_themes), 1)
    pts = round(theme_ratio * 30, 1)

    if matched_themes:
        top = sorted(matched_themes)[:4]
        rationale = f"themes matched: {', '.join(top)}"
    else:
        rationale = "no theme overlap"
    return pts, rationale


def _seniority_score(job_title: str, target_levels: list[str]) -> tuple[float, str]:
    """20 pts. Checks for seniority keywords in job title."""
    if not job_title or not target_levels:
        return 10.0, "no seniority data (neutral)"  # neutral half-credit

    title_lower = job_title.lower()
    level_keywords = {
        "junior": ["junior", "jr", "associate", "entry"],
        "mid": ["product manager", "pm "],
        "senior": ["senior", "sr.", "sr ", "staff"],
        "principal": ["principal", "distinguished"],
        "lead": ["lead", "tech lead"],
        "director": ["director", "vp", "vice president", "head of"],
        "group": ["group", "gpm"],
        "head": ["head of", "head,"],
    }

    matched_levels = []
    for level in target_levels:
        keywords = level_keywords.get(level.lower(), [level.lower()])
        if any(kw in title_lower for kw in keywords):
            matched_levels.append(level)

    if matched_levels:
        return 20.0, f"seniority match ({', '.join(matched_levels)})"

    # Penalize if an explicitly non-target level is detected
    if any(kw in title_lower for kw in ["junior", "jr.", "jr ", "associate", "entry-level"]):
        return 0.0, "junior/entry-level role (below target)"

    # Unknown — give neutral half-credit
    return 10.0, "seniority unclear (neutral)"


def _salary_score(
    salary_min: Optional[float],
    salary_max: Optional[float],
    target_min: Optional[float],
    target_max: Optional[float],
) -> tuple[float, str]:
    """10 pts. Full credit if no salary posted (don't penalize missing data)."""
    if salary_min is None and salary_max is None:
        return 10.0, "salary not listed (neutral)"

    if target_min is None and target_max is None:
        return 10.0, "no target salary set (neutral)"

    # Use midpoint of posted range
    posted_mid = (salary_min or salary_max) + (salary_max or salary_min)
    posted_mid /= 2 if (salary_min and salary_max) else 1

    t_min = target_min or 0
    t_max = target_max or float("inf")

    if t_min <= posted_mid <= t_max:
        return 10.0, f"salary ${posted_mid:,.0f} in target range"
    elif posted_mid < t_min:
        gap_pct = (t_min - posted_mid) / t_min
        pts = round(max(0.0, 10.0 * (1 - gap_pct * 2)), 1)
        return pts, f"salary ${posted_mid:,.0f} below target (${t_min:,.0f})"
    else:
        return 10.0, f"salary ${posted_mid:,.0f} above target (bonus)"


def _location_score(
    is_remote: bool,
    location: str,
    preferred_remote: bool,
    preferred_locations: list[str],
) -> tuple[float, str]:
    """10 pts."""
    if preferred_remote and is_remote:
        return 10.0, "remote ✓"

    loc_lower = (location or "").lower()
    for pref in preferred_locations:
        if pref.lower() in loc_lower or loc_lower in pref.lower():
            return 10.0, f"location match ({pref})"

    if is_remote:
        return 8.0, "remote (not in preferred locations but acceptable)"

    return 2.0, "location mismatch"


@dataclass
class ScoreResult:
    score: float
    rationale: str


def score_job(row: sqlite3.Row, profile: dict, skills_bank: dict) -> ScoreResult:
    """Score a single job row against profile + skills bank. Returns 0-100."""
    title = row["title"] or ""
    description = row["description"] or ""
    is_remote = bool(row["is_remote"])
    location = row["location"] or ""
    salary_min = row["salary_min"]
    salary_max = row["salary_max"]

    target_titles = profile.get("target_titles", [])
    target_levels = profile.get("target_levels", [])
    target_salary_min = profile.get("target_salary_min")
    target_salary_max = profile.get("target_salary_max")
    preferred_remote = profile.get("preferred_remote", True)
    preferred_locations = profile.get("preferred_locations", [])
    avoid_keywords = profile.get("avoid_keywords", [])

    t_pts, t_note = _title_score(title, target_titles)
    s_pts, s_note = _skills_score(description, skills_bank)
    sn_pts, sn_note = _seniority_score(title, target_levels)
    sal_pts, sal_note = _salary_score(salary_min, salary_max, target_salary_min, target_salary_max)
    loc_pts, loc_note = _location_score(is_remote, location, preferred_remote, preferred_locations)

    conn_count = (row["connection_count"] if "connection_count" in row.keys() else 0) or 0
    if conn_count >= 5:
        conn_pts = 10
    elif conn_count >= 2:
        conn_pts = 8
    elif conn_count >= 1:
        conn_pts = 5
    else:
        conn_pts = 0

    raw = t_pts + s_pts + sn_pts + sal_pts + loc_pts + conn_pts

    # avoid_keywords penalty
    penalty_triggered = []
    desc_lower = (title + " " + description).lower()
    for kw in avoid_keywords:
        if kw.lower() in desc_lower:
            penalty_triggered.append(kw)

    if penalty_triggered:
        raw = raw * 0.5

    score = round(min(raw, 100.0), 1)

    parts = [t_note, s_note, sn_note]
    if sal_pts < 10:
        parts.append(sal_note)
    if loc_pts < 10:
        parts.append(loc_note)
    if conn_pts > 0:
        parts.append(f"connection boost (+{conn_pts}, {conn_count} connection{'s' if conn_count != 1 else ''})")
    if penalty_triggered:
        parts.append(f"penalty: {penalty_triggered[0]!r}")

    rationale = " | ".join(parts)
    return ScoreResult(score=score, rationale=rationale)


def score_all(conn: sqlite3.Connection, profile: dict, skills_bank: dict) -> int:
    """Score all unscored jobs in the DB. Returns count of jobs scored."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE score IS NULL AND (status IS NULL OR status != 'dismissed')"
    ).fetchall()

    count = 0
    for row in rows:
        result = score_job(row, profile, skills_bank)
        conn.execute(
            "UPDATE jobs SET score = ?, score_rationale = ? WHERE id = ?",
            (result.score, result.rationale, row["id"]),
        )
        count += 1

    return count
