# job_puller

A local job scraper, scorer, and tracker. It pulls listings from LinkedIn, Indeed, and optional configurable sources (Tangerine Feed, PM Job Board), scores each one against your resume and preferences, and serves a live dashboard where you can browse, filter, tailor, and track applications — all without sending your data anywhere.

Everything runs on your machine. No external APIs, no cloud storage, no subscriptions.

---

## How it works

```
Scrape  →  Score  →  Rank  →  Web UI
```

1. **Scrape** — pulls listings from job boards based on your search terms and locations
2. **Score** — heuristic 0–100 score per job: title match, skills overlap, seniority, salary, location, and network connections
3. **Rank** — top N jobs by score, excluding dismissed roles
4. **Web UI** — live dashboard at `localhost:5000` with filters, tailor/interview prompts, apply/dismiss tracking

---

## Requirements

- Python 3.11+
- pip

---

## Setup

```bash
# 1. Clone
git clone https://github.com/yourname/job_puller.git
cd job_puller

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install
pip install -e ".[dev]"

# 4. Copy and fill in the config files
cp config/config.template.yaml config/config.yaml
cp profile/profile.template.yaml profile/profile.yaml
cp profile/skills_bank.template.yaml profile/skills_bank.yaml

# 5. Edit each file — instructions are inline
$EDITOR config/config.yaml
$EDITOR profile/profile.yaml
$EDITOR profile/skills_bank.yaml
```

---

## Configuration

### `config/config.yaml`

Controls where to scrape, what to search for, and how many results to keep.

| Key | What to set |
|-----|-------------|
| `data_path` | Where the database and logs are stored. Default: `~/.job_puller` |
| `search.terms` | Job titles to search for. Use your actual target titles. |
| `search.locations` | Cities + "Remote". Passed directly to job boards. |
| `search.allowed_locations` | On-site jobs are only kept if their location contains one of these strings. |
| `search.hours_old` | Only fetch listings posted in the last N hours. |
| `report.top_n` | How many jobs to show in the dashboard and digest. |

### `profile/profile.yaml`

Controls how jobs are scored against your preferences.

| Key | What to set |
|-----|-------------|
| `target_titles` | Titles you're targeting — used for title match scoring |
| `target_levels` | Seniority levels you want: `senior`, `mid`, `lead`, `principal`, etc. |
| `target_salary_min/max` | Your acceptable salary range |
| `preferred_remote` | `true` if you prefer remote |
| `preferred_locations` | On-site locations you'd consider |
| `avoid_keywords` | Words that trigger a 0.5× score penalty (e.g. "sales", "hardware") |
| `exclude_title_keywords` | Titles to exclude entirely from results |
| `prompt_context.role_description` | How you describe yourself in tailor/interview prompts — e.g. `"a Senior Product Manager"` |
| `prompt_context.tailor_guidance` | List of extra instruction lines appended to the tailor prompt |
| `prompt_context.interview_guidance` | List of extra instruction lines appended to the interview prep prompt |

### `profile/skills_bank.yaml`

Your resume content. Used for skills overlap scoring, bullet matching in tailor prompts, and recommended summary variants. See the template for the full structure.

---

## Commands

```bash
# Pull new jobs, score them, and update the database
job-puller run

# Open the web dashboard at http://localhost:5000
job-puller serve

# Re-score all jobs (use after editing profile.yaml or skills_bank.yaml)
job-puller rescore

# Print top jobs in the terminal
job-puller digest

# Install a cron job to run automatically at 7 AM daily
job-puller schedule

# Remove the cron job
job-puller schedule --uninstall

# Open profile.yaml in $EDITOR
job-puller profile edit

# Open skills_bank.yaml in $EDITOR
job-puller profile skills

# Add a network connection (boosts score for matching companies)
# Use the web UI: http://localhost:5000/connections
```

---

## Scoring (0–100)

Each job is scored against your profile using weighted heuristics. No AI calls — pure local computation.

| Component | Weight | Notes |
|-----------|--------|-------|
| Title match | 30 pts | Token overlap between job title and your `target_titles` |
| Skills overlap | 30 pts | Job description themes matched against your skills bank |
| Seniority match | 20 pts | Job title vs. your `target_levels`; neutral 10 pts if unclear |
| Salary fit | 10 pts | Posted salary vs. your `target_salary_min/max`; neutral if no salary listed |
| Location/remote | 10 pts | Remote preference + `preferred_locations` |
| Connection boost | +5/8/10 | +5 for 1 connection at company, +8 for 2–4, +10 for 5+ |

`avoid_keywords` in your profile apply a 0.5× multiplier to the final score.

---

## Web UI

Start the server:
```bash
job-puller serve
```

Opens at `http://localhost:5000`. Features:

- **Filter bar** — search by title/company, filter by source, theme, industry, or run date
- **Start Job Pull** — run the full scrape pipeline without leaving the browser; shows live progress
- **Apply / Dismiss** — updates the database; dismissed jobs are hidden, applied jobs move to a separate section
- **Tailor ✦** — generates a ready-to-paste prompt for tailoring your resume to the specific job
- **Interview ✦** — generates interview prep materials for the role
- **Connections ⬡** — manage your network connections list
- **Dark mode** — toggle in the top-right corner; respects your OS setting by default

---

## Network Connections

Jobs at companies where you have a connection get a score boost. Add connections at `http://localhost:5000/connections` or via the **Connections ⬡** link in the dashboard header.

Matching is **case-insensitive exact match** on company name. If you add "Stripe" as a connection and a job is listed under "stripe", it matches. "Stripe, Inc." does not — enter the company name exactly as it appears on the job posting.

After adding connections, run `job-puller rescore` to apply the boost to existing jobs.

---

## Daily automation

```bash
# Install — runs job-puller at 7 AM every day via cron
job-puller schedule
```

This requires a separate venv outside `~/Documents` due to macOS Full Disk Access restrictions on cron:

```bash
python3 -m venv ~/.job_puller/venv
~/.job_puller/venv/bin/pip install -e /path/to/job_puller
job-puller schedule
```

Logs are written to `~/.job_puller/logs/daily.log`.

> **Note:** cron only runs when your Mac is awake. If the Mac is asleep at 7 AM, the run is skipped. Use the **Start Job Pull** button in the web UI to run manually.

---

## Data & privacy

- All data is stored locally in `~/.job_puller/jobs.db` (SQLite)
- No data is sent to any external service — scraping only reads from job boards
- Your resume content (`skills_bank.yaml`), preferences (`profile.yaml`), and config (`config.yaml`) are gitignored and never committed
- The tailor and interview prompts are assembled locally and printed for you to paste into an AI tool manually

---

## Development

```bash
# Run tests
pytest

# Lint
ruff check .

# Type check
mypy job_puller/
```

---

## Project structure

```
job_puller/
├── job_puller/          # Python package
│   ├── cli.py           # Typer CLI entry point
│   ├── scraper.py       # JobSpy wrapper (LinkedIn, Indeed)
│   ├── tangerine.py     # Tangerine Feed scraper (configurable URL)
│   ├── pmjb.py          # productmanagerjobboard.com scraper (optional, disable in config)
│   ├── db.py            # SQLite schema, migrations, queries
│   ├── scorer.py        # Heuristic 0-100 scorer
│   ├── ranker.py        # Filter + sort for display
│   ├── resume_matcher.py# Bullet/summary matching
│   ├── industry_classifier.py
│   ├── location_flags.py
│   ├── connections.py   # Network connection matching
│   ├── server.py        # Flask web UI
│   └── reporter.py      # Static HTML report generator
├── templates/           # Jinja2 templates
├── profile/             # Your resume content (gitignored)
│   ├── profile.template.yaml
│   └── skills_bank.template.yaml
├── config/              # Scraper configuration (gitignored)
│   └── config.template.yaml
└── tests/
```
