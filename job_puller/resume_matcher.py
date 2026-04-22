"""Resume matcher — scores skills_bank bullets against a job description.

Pure Python, no API calls. Reuses scorer._tokenize for consistency.

Algorithm per bullet:
  +2 per theme tag that appears in the job description tokens
  +1 per distinct word from bullet text that appears in job description tokens
  × strength weight (high=1.0, medium=0.75, supporting=0.5)

Summary variant selection:
  Each summary_variant key maps to a theme name. The variant whose theme
  name has the most token overlap with the job description wins.
"""

import json
import re
import sqlite3
from typing import Optional

from job_puller.scorer import _tokenize

_STRENGTH_WEIGHTS = {"high": 1.0, "medium": 0.75, "supporting": 0.5}

# Map summary variant keys to the themes they represent for overlap scoring
_SUMMARY_THEME_MAP = {
    "data": ["data", "analytics", "self-serve"],
    "technical": ["technical", "developer-platform"],
    "api": ["api", "developer-platform", "technical"],
    "compliance": ["compliance", "fintech"],
    "growth": ["growth", "startup"],
    "customer": ["customer", "leadership"],
    "execution": ["leadership", "execution"],
}


def extract_jd_highlights(description: str, skills_bank: dict, top_n: int = 3) -> list[str]:
    """Extract the most relevant bullet-like lines from a job description.

    Parses lines that look like bullet points, scores them by overlap with
    skills bank themes, and returns the top N truncated to 120 chars.
    """
    if not description:
        return []

    all_themes: set[str] = set()
    for b in skills_bank.get("bullets", []):
        all_themes.update(b.get("themes", []))

    candidates: list[tuple[float, str]] = []
    for raw_line in description.splitlines():
        # Strip bullet markers: *, -, •, digits followed by . or )
        line = raw_line.strip()
        line = re.sub(r"^[\*\-\•]\s*|^\d+[\.\)]\s*", "", line).strip()
        # Remove markdown bold/italic
        line = re.sub(r"\*+", "", line).strip()
        if len(line) < 30 or len(line) > 300:
            continue
        tokens = _tokenize(line)
        score = sum(
            1 for t in all_themes
            if t in tokens or t.replace("-", "") in tokens
            or any(part in tokens for part in t.split("-"))
        )
        if score > 0:
            candidates.append((score, line))

    candidates.sort(reverse=True)
    seen: list[str] = []
    for _, line in candidates:
        truncated = line[:120] + ("…" if len(line) > 120 else "")
        seen.append(truncated)
        if len(seen) >= top_n:
            break
    return seen


def match_job(description: str, skills_bank: dict, top_n: int = 10) -> tuple[list[str], str]:
    """Return (top bullet IDs, best summary variant label) for a job description."""
    if not description or not skills_bank:
        return [], ""

    desc_tokens = _tokenize(description)
    bullets = skills_bank.get("bullets", [])

    scored: list[tuple[float, str]] = []
    for bullet in bullets:
        bid = bullet.get("id", "")
        themes = bullet.get("themes", [])
        text = bullet.get("text", "")
        strength = bullet.get("strength", "medium")
        weight = _STRENGTH_WEIGHTS.get(strength, 0.5)

        theme_score = sum(
            2 for t in themes
            if t in desc_tokens
            or t.replace("-", "") in desc_tokens
            or any(part in desc_tokens for part in t.split("-"))
        )
        text_tokens = _tokenize(text)
        text_score = len(text_tokens & desc_tokens)

        total = (theme_score + text_score) * weight
        if total > 0:
            scored.append((total, bid))

    scored.sort(reverse=True)
    top_ids = [bid for _, bid in scored[: max(top_n, 8)]]

    # Pick best summary variant.
    # Variant keys that match their own name as a token get a +2 bonus
    # (e.g. "api" variant gets +2 if "api" appears in the job description).
    # This ensures strongly-signaled variants beat generic ones.
    summary_variants = skills_bank.get("summary_variants", {})
    best_variant = ""
    best_overlap = -1
    for variant_key, themes in _SUMMARY_THEME_MAP.items():
        if variant_key not in summary_variants:
            continue
        overlap = sum(
            1 for t in themes
            if t in desc_tokens
            or t.replace("-", "") in desc_tokens
            or any(part in desc_tokens for part in t.split("-"))
        )
        # Self-signal bonus: if the variant key itself appears in the job description
        if variant_key in desc_tokens:
            overlap += 2
        if overlap > best_overlap:
            best_overlap = overlap
            best_variant = variant_key

    return top_ids, best_variant


def match_all(conn: sqlite3.Connection, skills_bank: dict) -> int:
    """Run resume matching on all jobs without matched_bullet_ids. Returns count processed."""
    rows = conn.execute(
        """
        SELECT id, description FROM jobs
        WHERE matched_bullet_ids IS NULL
          AND description IS NOT NULL
          AND description != ''
          AND (status IS NULL OR status != 'dismissed')
        """
    ).fetchall()

    count = 0
    for row in rows:
        bullet_ids, summary_variant = match_job(row["description"], skills_bank)
        highlights = extract_jd_highlights(row["description"], skills_bank)
        conn.execute(
            "UPDATE jobs SET matched_bullet_ids = ?, recommended_summary = ?, description_highlights = ? WHERE id = ?",
            (json.dumps(bullet_ids), summary_variant, json.dumps(highlights), row["id"]),
        )
        count += 1

    return count
