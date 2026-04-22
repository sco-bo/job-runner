"""CLI entry point — Typer app with all job-puller commands."""

import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import typer
import yaml

from job_puller import connections, db, industry_classifier, location_flags, pmjb, ranker, reporter, resume_matcher, scraper, scorer, tangerine

app = typer.Typer(help="Daily job scraper and ranker.", add_completion=False)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "config.yaml"
_PROFILE_PATH = Path(__file__).parent.parent / "profile" / "profile.yaml"
_SKILLS_BANK_PATH = Path(__file__).parent.parent / "profile" / "skills_bank.yaml"
_PROFILE_TEMPLATE = Path(__file__).parent.parent / "profile" / "profile.template.yaml"
_SKILLS_TEMPLATE = Path(__file__).parent.parent / "profile" / "skills_bank.template.yaml"


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _require_profile_files() -> tuple[dict, dict]:
    """Load profile.yaml and skills_bank.yaml or exit with a clear error."""
    if not _PROFILE_PATH.exists():
        typer.echo(
            f"Error: {_PROFILE_PATH} not found. "
            f"Copy {_PROFILE_TEMPLATE} to get started.",
            err=True,
        )
        raise typer.Exit(1)
    if not _SKILLS_BANK_PATH.exists():
        typer.echo(
            f"Error: {_SKILLS_BANK_PATH} not found. "
            f"Copy {_SKILLS_TEMPLATE} to get started.",
            err=True,
        )
        raise typer.Exit(1)
    return _load_yaml(_PROFILE_PATH), _load_yaml(_SKILLS_BANK_PATH)


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        typer.echo(f"Error: {_CONFIG_PATH} not found.", err=True)
        raise typer.Exit(1)
    return _load_yaml(_CONFIG_PATH)


def _get_db_path(config: dict) -> Path:
    data_path = config.get("data_path", "~/.job_puller")
    return db.get_db_path(data_path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def run(
    open_report: bool = typer.Option(False, "--open", help="Open HTML report in browser after run."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scrape jobs from all sources and store new ones in the database."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    _require_profile_files()
    config = _load_config()
    db_path = _get_db_path(config)
    db.init_db(db_path)

    with db.connect(db_path) as conn:
        run_id = db.start_run(conn)

    jobs_fetched = 0
    jobs_new = 0

    # --- JobSpy ---
    spy_jobs = scraper.scrape(config)
    jobs_fetched += len(spy_jobs)
    with db.connect(db_path) as conn:
        for job in spy_jobs:
            if db.upsert_job(conn, job, run_id):
                jobs_new += 1

    # --- Tangerine ---
    tan_jobs = tangerine.scrape(config)
    jobs_fetched += len(tan_jobs)
    with db.connect(db_path) as conn:
        for job in tan_jobs:
            if db.upsert_job(conn, job, run_id):
                jobs_new += 1

    # --- PM Job Board ---
    pmjb_jobs = pmjb.scrape(config)
    jobs_fetched += len(pmjb_jobs)
    with db.connect(db_path) as conn:
        for job in pmjb_jobs:
            if db.upsert_job(conn, job, run_id):
                jobs_new += 1

    with db.connect(db_path) as conn:
        db.finish_run(conn, run_id, jobs_fetched, jobs_new)

    typer.echo(f"Scraped: {jobs_fetched} fetched, {jobs_new} new.")

    # --- Score + match new jobs ---
    profile, skills_bank = _require_profile_files()
    with db.connect(db_path) as conn:
        n_connections = connections.match_all(conn)
        n_scored = scorer.score_all(conn, profile, skills_bank)
        n_matched = resume_matcher.match_all(conn, skills_bank)
        n_classified = industry_classifier.classify_all(conn)
        n_flagged = location_flags.flag_all(conn)
    typer.echo(f"Scored:  {n_scored} jobs, matched: {n_matched} jobs, classified: {n_classified} industries, flagged: {n_flagged} state-restricted, connections: {n_connections}.")

    if open_report:
        from pathlib import Path as _Path
        reports_dir = _Path(__file__).parent.parent / "reports"
        with db.connect(db_path) as conn:
            exclude_kw = profile.get("exclude_title_keywords", [])
            top = ranker.get_top_jobs(conn, config.get("report", {}).get("top_n", 20), exclude_kw)
            applied = ranker.get_applied_jobs(conn)
            stats = db.get_run_stats(conn)
        report_path = reporter.generate_report(top, applied, stats, skills_bank, reports_dir)
        typer.echo(f"Report:  {report_path}")
        import webbrowser
        webbrowser.open(report_path.as_uri())


@app.command()
def rescore(
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-run scoring and resume matching on all jobs (use after editing profile or skills bank)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    config = _load_config()
    db_path = _get_db_path(config)

    if not db_path.exists():
        typer.echo("No database found. Run `job-puller run` first.", err=True)
        raise typer.Exit(1)

    db.init_db(db_path)  # applies any pending migrations
    profile, skills_bank = _require_profile_files()

    with db.connect(db_path) as conn:
        # Clear existing scores, match data, and industry so everything is reprocessed
        conn.execute("UPDATE jobs SET score = NULL, score_rationale = NULL, matched_bullet_ids = NULL, recommended_summary = NULL, industry = NULL")
    typer.echo("Cleared existing scores and match data.")

    with db.connect(db_path) as conn:
        n_connections = connections.match_all(conn)
        n_scored = scorer.score_all(conn, profile, skills_bank)
        n_matched = resume_matcher.match_all(conn, skills_bank)
        n_classified = industry_classifier.classify_all(conn)
        n_flagged = location_flags.flag_all(conn)
    typer.echo(f"Rescored: {n_scored} jobs, matched: {n_matched} jobs, classified: {n_classified} industries, flagged: {n_flagged} state-restricted, connections: {n_connections}.")


@app.command()
def digest(
    top_n: int = typer.Option(20, "--top", "-n", help="Number of jobs to show."),
) -> None:
    """Show top ranked jobs in the terminal (excludes dismissed and applied)."""
    config = _load_config()
    db_path = _get_db_path(config)

    if not db_path.exists():
        typer.echo("No database found. Run `job-puller run` first.", err=True)
        raise typer.Exit(1)

    profile, _ = _require_profile_files()
    exclude_kw = profile.get("exclude_title_keywords", [])

    with db.connect(db_path) as conn:
        top = ranker.get_top_jobs(conn, top_n, exclude_kw)
        applied = ranker.get_applied_jobs(conn)
        stats = db.get_run_stats(conn)

    if not top:
        typer.echo("No scored jobs found. Run `job-puller run` first.")
        return

    typer.echo(f"\n{'─' * 72}")
    typer.echo(f"  TOP {len(top)} JOBS  (total: {stats['total']} | dismissed: {stats['dismissed']})")
    typer.echo(f"{'─' * 72}")

    for i, job in enumerate(top, 1):
        score_color = (
            typer.colors.GREEN if job.score >= 80
            else typer.colors.YELLOW if job.score >= 60
            else typer.colors.RED
        )
        score_str = typer.style(f"{job.score:5.1f}", fg=score_color, bold=True)
        remote_tag = " [remote]" if job.is_remote else ""
        salary = ""
        if job.salary_min or job.salary_max:
            lo = f"${job.salary_min/1000:.0f}k" if job.salary_min else "?"
            hi = f"${job.salary_max/1000:.0f}k" if job.salary_max else "?"
            salary = f"  {lo}–{hi}"

        typer.echo(f"\n{i:2}. {score_str}  {job.title}")
        typer.echo(f"      {job.company}{remote_tag}{salary}  [{job.site}] #{job.id}")
        typer.echo(f"      {job.score_rationale[:80]}")
        typer.echo(f"      {job.url}")

    typer.echo(f"\n{'─' * 72}")
    typer.echo(f"  Applied: {len(applied)} job(s)  |  Run `job-puller tailor <id>` to generate a tailoring prompt")
    typer.echo(f"{'─' * 72}\n")


@app.command()
def search(query: str = typer.Argument(..., help="Search term")) -> None:
    """Search all jobs in the database by keyword."""
    typer.echo("(search not yet implemented — coming in Phase 4)")


@app.command()
def tailor(job_id: int = typer.Argument(..., help="Job ID from the database")) -> None:
    """Print a ready-to-paste Claude.ai prompt for resume tailoring."""
    typer.echo("(tailor not yet implemented — coming in Phase 4)")


@app.command(name="job")
def job_status(
    job_ref: str = typer.Argument(..., help="Job ID or URL"),
    status: str = typer.Option(..., help="applied or dismissed"),
) -> None:
    """Mark a job as applied or dismissed."""
    typer.echo("(job status not yet implemented — coming in Phase 4)")


@app.command()
def serve(
    port: int = typer.Option(5000, "--port", "-p", help="Port to listen on."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open browser automatically."),
) -> None:
    """Start the local job browser UI at http://localhost:<port>."""
    import threading
    import webbrowser
    from job_puller.server import app as flask_app

    _require_profile_files()
    config = _load_config()
    profile, skills_bank = _require_profile_files()
    db_path = _get_db_path(config)

    if not db_path.exists():
        typer.echo("No database found. Run `job-puller run` first.", err=True)
        raise typer.Exit(1)

    db.init_db(db_path)  # ensure all migrations (incl. connections table) are applied
    flask_app.config["DB_PATH"] = db_path
    flask_app.config["SKILLS_BANK"] = skills_bank
    flask_app.config["TOP_N"] = config.get("report", {}).get("top_n", 20)
    flask_app.config["CONFIG"] = config

    url = f"http://127.0.0.1:{port}"
    if not no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    typer.echo(f"Starting job browser at {url} — press Ctrl+C to stop.")
    flask_app.run(host="127.0.0.1", port=port, debug=False)


@app.command()
def schedule(
    uninstall: bool = typer.Option(False, "--uninstall", help="Remove the scheduled cron job."),
) -> None:
    """Install (or remove) a cron job that runs job-puller at 7 AM daily."""
    import subprocess
    import sys

    log_dir = Path.home() / ".job_puller" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "daily.log"

    # Always use the venv outside ~/Documents — the project venv can't be read by cron
    # due to macOS FDA restrictions on ~/Documents.
    job_puller_bin = Path.home() / ".job_puller" / "venv" / "bin" / "job-puller"
    if not uninstall and not job_puller_bin.exists():
        typer.echo(
            f"Error: {job_puller_bin} not found.\n"
            "Create it with: python3 -m venv ~/.job_puller/venv && "
            "~/.job_puller/venv/bin/pip install -e /path/to/job_puller",
            err=True,
        )
        raise typer.Exit(1)
    cron_line = f"0 7 * * * {job_puller_bin} run >> {log_path} 2>&1"
    marker = "# job-puller-daily"
    cron_entry = f"{cron_line}  {marker}"

    # Read existing crontab
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = result.stdout if result.returncode == 0 else ""

    # Strip any previous job-puller entry
    lines = [l for l in existing.splitlines() if marker not in l]

    if uninstall:
        if marker in existing:
            new_crontab = "\n".join(lines) + ("\n" if lines else "")
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            typer.echo("Uninstalled: daily job-puller cron job removed.")
        else:
            typer.echo("Not installed — nothing to remove.")
        return

    lines.append(cron_entry)
    new_crontab = "\n".join(lines) + "\n"
    subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)

    typer.echo("Installed: job-puller will run daily at 7:00 AM via cron.")
    typer.echo(f"Logs: {log_path}")
    typer.echo("To remove: job-puller schedule --uninstall")


profile_app = typer.Typer(help="View and edit your profile and skills bank.")
app.add_typer(profile_app, name="profile")


@profile_app.command(name="edit")
def profile_edit() -> None:
    """Open profile.yaml in $EDITOR."""
    import os
    import subprocess
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(_PROFILE_PATH)])


@profile_app.command(name="skills")
def profile_skills() -> None:
    """Open skills_bank.yaml in $EDITOR."""
    import os
    import subprocess
    editor = os.environ.get("EDITOR", "vi")
    subprocess.run([editor, str(_SKILLS_BANK_PATH)])
