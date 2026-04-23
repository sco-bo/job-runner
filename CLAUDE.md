# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

Daily job scraper that fetches listings from configurable sources, scores them against a local skills bank, and outputs a ranked digest. All logic is local; no external APIs are called at runtime. The full spec is in [spec/spec.md](spec/spec.md) — read it before making architectural decisions.

## Commands

```bash
# Install dependencies (once venv is set up)
pip install -e ".[dev]"

# Run the full pipeline
job-puller run

# Run tests
pytest

# Run a single test file
pytest tests/test_scorer.py

# Run a single test
pytest tests/test_scorer.py::test_title_match

# Lint
ruff check .

# Type check
mypy job_puller/
```

## Architecture

The app is a Python CLI (`job_puller/` package, entry point in `cli.py` via Typer). The pipeline flows:

1. **Scraping** — two independent scrapers write to the same SQLite `jobs` table:
   - `scraper.py` wraps JobSpy (LinkedIn/Indeed/Glassdoor)
   - `tangerine.py` scrapes a configurable Tangerine Feed URL via JSON-LD (`schema.org/ItemList`) — does NOT scrape HTML. Paginates with `?page=N&sort=recent`, stops when all jobs on a page are older than `hours_old`. Skipped if `base_url` is blank.

2. **Scoring** (`scorer.py`) — pure Python, no API calls. Weighted heuristics: title match (30), skills overlap (30), seniority match (20), salary fit (10), location/remote (10). `avoid_keywords` apply a 0.5× penalty. Skills overlap checks bullet `themes` tags and raw bullet `text` in `skills_bank.yaml`.

3. **Ranking** (`ranker.py`) — filters out `status = "dismissed"` jobs entirely, excludes `status = "applied"` from the top-20 pool, then sorts by score descending.

4. **Resume matching** (`resume_matcher.py`) — tokenizes job description, scores every bullet in `skills_bank.yaml` by theme overlap (+2/theme) and word overlap (+1/word) weighted by strength (`high`=1.0, `medium`=0.75, `supporting`=0.5). Returns top 8–10 bullet IDs and best summary variant label. Stored in the `jobs` table as `matched_bullet_ids` (JSON array) and `recommended_summary`.

5. **Reporting** (`reporter.py`) — Jinja2 HTML digest. Top 20 as scored cards; applied jobs in a separate muted section below; dismissed jobs never rendered.

6. **Tailor command** (`cli.py`) — assembles a plain-text prompt from the job + matched bullets + recommended summary and prints to stdout. Zero API calls. User pastes it into Claude.ai manually.

## Key constraints

- **No Anthropic SDK, no API calls, ever.** The tool is fully offline except for scraping. The `tailor` command only prints a string — it does not call any AI service.
- **`skills_bank.yaml` is user-owned.** The tool reads it but never writes or generates it. If it's missing, exit immediately with a descriptive error pointing to `profile/skills_bank.template.yaml`.
- Same rule applies to `profile.yaml` — missing file = immediate exit, no interactive prompting.
- Dedup key is `url` (UNIQUE constraint in SQLite). Both scrapers rely on this — never change it to an auto-increment or content hash.

## Data files

| File | Purpose |
|------|---------|
| `profile/profile.yaml` | Search prefs, target titles, salary range, seniority, avoid_keywords |
| `profile/skills_bank.yaml` | All resume content: summary variants, bullets (id/text/themes/strength), education, certs |
| `config/config.yaml` | Scraper params, Tangerine config, report settings |
| `~/.job_puller/jobs.db` (default) | SQLite database; path overridable via `data_path` in config |

## Build phases

Development follows the phased plan in the spec. Don't jump ahead:
1. Scrape + Store (both scrapers → SQLite, dedup verified)
2. Score + Rank (heuristics, terminal digest)
3. Resume Matching + HTML Report
4. Profile CLI + Tailor Command
