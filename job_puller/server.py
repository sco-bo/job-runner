"""Flask local server — live job browser with apply/dismiss/restore actions."""

import dataclasses
import json as _json
import logging
import queue
import threading
import urllib.request
import uuid
from datetime import date
from pathlib import Path

import yaml
from flask import Flask, abort, redirect, render_template, request, url_for

from job_puller import (
    connections, db, industry_classifier, location_flags, pmjb,
    ranker, resume_matcher, scorer, scraper, tangerine,
)
from job_puller.db import get_runs
from job_puller.reporter import _build_bullets_map

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_PROFILE_PATH = Path(__file__).parent.parent / "profile" / "profile.yaml"

app = Flask(__name__, template_folder=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Job-pull run state (SSE streaming)
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _RunState:
    status: str = "idle"   # "idle" | "running" | "done" | "error"
    queue: queue.Queue = dataclasses.field(default_factory=queue.Queue)
    result: dict = dataclasses.field(default_factory=dict)

_run_state = _RunState()
_run_lock  = threading.Lock()


class _SSELogHandler(logging.Handler):
    _WATCHED = [
        "job_puller.scraper", "job_puller.tangerine", "job_puller.pmjb",
        "job_puller.scorer", "job_puller.resume_matcher",
        "job_puller.industry_classifier", "job_puller.location_flags",
    ]

    def __init__(self, state: _RunState):
        super().__init__(level=logging.INFO)
        self._state = state

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._state.queue.put_nowait({"event": "log", "data": self.format(record)})
        except Exception:
            pass

    def attach(self) -> None:
        for name in self._WATCHED:
            logging.getLogger(name).addHandler(self)

    def detach(self) -> None:
        for name in self._WATCHED:
            logging.getLogger(name).removeHandler(self)


def _pipeline_worker() -> None:
    state = _run_state
    handler = _SSELogHandler(state)
    handler.attach()

    def _put(event: str, data) -> None:
        state.queue.put_nowait({"event": event, "data": data})

    try:
        config: dict = app.config["CONFIG"]
        db_path: Path = app.config["DB_PATH"]
        skills_bank: dict = app.config["SKILLS_BANK"]

        profile: dict = {}
        if _PROFILE_PATH.exists():
            with open(_PROFILE_PATH) as f:
                profile = yaml.safe_load(f) or {}

        db.init_db(db_path)
        with db.connect(db_path) as conn:
            run_id = db.start_run(conn)

        jobs_fetched = 0
        jobs_new = 0

        # JobSpy
        _put("stage", {"stage": "jobspy", "label": "Scraping JobSpy…"})
        spy_jobs = scraper.scrape(config)
        jobs_fetched += len(spy_jobs)
        with db.connect(db_path) as conn:
            for job in spy_jobs:
                if db.upsert_job(conn, job, run_id):
                    jobs_new += 1

        # Tangerine
        _put("stage", {"stage": "tangerine", "label": "Scraping Tangerine…"})
        tan_jobs = tangerine.scrape(config)
        jobs_fetched += len(tan_jobs)
        with db.connect(db_path) as conn:
            for job in tan_jobs:
                if db.upsert_job(conn, job, run_id):
                    jobs_new += 1

        # PM Job Board
        _put("stage", {"stage": "pmjb", "label": "Scraping PM Job Board…"})
        pmjb_jobs = pmjb.scrape(config)
        jobs_fetched += len(pmjb_jobs)
        with db.connect(db_path) as conn:
            for job in pmjb_jobs:
                if db.upsert_job(conn, job, run_id):
                    jobs_new += 1

        with db.connect(db_path) as conn:
            db.finish_run(conn, run_id, jobs_fetched, jobs_new)

        # Score + match + classify + flag
        _put("stage", {"stage": "scoring", "label": "Scoring and matching…"})
        with db.connect(db_path) as conn:
            connections.match_all(conn)
            n_scored = scorer.score_all(conn, profile, skills_bank)
            n_matched = resume_matcher.match_all(conn, skills_bank)
            n_classified = industry_classifier.classify_all(conn)
            n_flagged = location_flags.flag_all(conn)

        state.result = {
            "jobs_fetched": jobs_fetched,
            "jobs_new": jobs_new,
            "scored": n_scored,
        }
        state.status = "done"
        _put("done", state.result)

    except Exception as exc:
        state.status = "error"
        _put("error", {"message": str(exc)})

    finally:
        handler.detach()
        state.queue.put(None)  # sentinel — closes SSE stream
        with _run_lock:
            if state.status == "running":
                state.status = "error"


@app.route("/api/run", methods=["POST"])
def api_run():
    from flask import jsonify
    with _run_lock:
        if _run_state.status == "running":
            return jsonify({"status": "already_running"}), 409
        _run_state.status = "running"
        _run_state.queue = queue.Queue()
        _run_state.result = {}
    threading.Thread(target=_pipeline_worker, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/run/stream")
def api_run_stream():
    def _generate():
        q = _run_state.queue
        while True:
            try:
                item = q.get(timeout=30)
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"
                continue
            if item is None:
                return
            event = item["event"]
            data = item["data"]
            if isinstance(data, dict):
                data = _json.dumps(data)
            yield f"event: {event}\ndata: {data}\n\n"

    return app.response_class(
        _generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _conn():
    """Open a connection using the db_path stored in app config."""
    db_path: Path = app.config["DB_PATH"]
    return db.connect(db_path)


def _exclude_keywords() -> list[str]:
    if _PROFILE_PATH.exists():
        with open(_PROFILE_PATH) as f:
            profile = yaml.safe_load(f) or {}
        return profile.get("exclude_title_keywords", [])
    return []


@app.route("/")
def index():
    exclude_kw = _exclude_keywords()

    run_id_param = request.args.get("run", type=int)

    with _conn() as conn:
        runs = get_runs(conn)
        top_jobs = ranker.get_top_jobs(
            conn, app.config.get("TOP_N", 20), exclude_kw, run_id=run_id_param
        )
        applied_jobs = ranker.get_applied_jobs(conn)
        dismissed_jobs = db.get_dismissed_jobs(conn)
        stats = db.get_run_stats(conn)

    skills_bank: dict = app.config.get("SKILLS_BANK", {})

    return render_template(
        "dashboard.html.j2",
        top_jobs=top_jobs,
        applied_jobs=applied_jobs,
        dismissed_jobs=dismissed_jobs,
        stats=stats,
        skills_bank_bullets=_build_bullets_map(skills_bank),
        is_server=True,
        generated_at=None,
        runs=runs,
        selected_run_id=run_id_param,
    )


def _push_to_job_search(job_id: int, job_row) -> None:
    """Fire-and-forget POST to job-search API; stores returned ID in SQLite."""
    config: dict = app.config.get("CONFIG", {})
    api_base = config.get("job_search_api", "").rstrip("/")
    if not api_base:
        return
    db_path = app.config["DB_PATH"]
    payload = _json.dumps({
        "jobTitle": job_row["title"] or "",
        "companyName": job_row["company"] or "",
        "jobUrl": job_row["url"] or "",
        "dateApplied": date.today().isoformat(),
        "applicationMethod": "online",
        "status": "applied",
    }).encode()

    def _send():
        try:
            req = urllib.request.Request(
                f"{api_base}/api/applications",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=3)
            created = _json.loads(resp.read())
            js_id = created.get("id")
            if js_id:
                with db.connect(db_path) as conn:
                    db.update_job_search_id(conn, job_id, js_id)
        except Exception:
            pass  # job_puller works fine if job-search is not running

    threading.Thread(target=_send, daemon=True).start()


def _delete_from_job_search(job_id: int, job_row) -> None:
    """Fire-and-forget DELETE to job-search API when a job is un-applied."""
    config: dict = app.config.get("CONFIG", {})
    api_base = config.get("job_search_api", "").rstrip("/")
    if not api_base:
        return
    js_id = job_row["job_search_id"] if "job_search_id" in job_row.keys() else None
    if not js_id:
        return
    db_path = app.config["DB_PATH"]

    def _send():
        try:
            req = urllib.request.Request(
                f"{api_base}/api/applications/{js_id}",
                method="DELETE",
            )
            urllib.request.urlopen(req, timeout=3)
            with db.connect(db_path) as conn:
                db.update_job_search_id(conn, job_id, None)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _build_tailor_prompt(job_row, skills_bank: dict) -> str:
    """Assemble the ready-to-paste Claude.ai prompt for a job."""
    import json

    title = job_row["title"] or ""
    company = job_row["company"] or ""
    url = job_row["url"] or ""
    description = job_row["description"] or ""
    recommended_summary = job_row["recommended_summary"] or ""

    summary_text = skills_bank.get("summary_variants", {}).get(recommended_summary, "")
    bullets_by_id = {b["id"]: b["text"] for b in skills_bank.get("bullets", [])}

    # Matched bullets (resume matcher top picks)
    raw_ids = job_row["matched_bullet_ids"]
    matched_ids = json.loads(raw_ids) if raw_ids else []
    matched_bullets = [bullets_by_id[bid] for bid in matched_ids if bid in bullets_by_id]

    # Roles section for full resume
    roles = skills_bank.get("roles", [])

    # Build per-role guidance lines for the guidance block
    role_limits = []
    for role in roles:
        role_limits.append(
            f"  - For {role['title']} at {role['company']}: keep {role['max_bullets']} bullets maximum"
        )

    lines = [
        "# Context",
        f"- I'm a Senior Product Manager applying for the role below.",
        f"- Below is my full master resume, organized by role with every bullet point.",
        f"- You are a hiring manager at {company} reviewing my application for this specific role.",
        f"- You are well-versed in best practices for tech PM resume writing.",
        "",
        "# Role",
        f"{title} at {company}",
        f"{url}",
        "",
        "# Job Description",
        description,
        "",
        "# Specific Guidance",
        "- Before recommending bullets, identify the primary evaluation lens this role uses to assess PM candidates. Do not default to surface-level keyword matching. Common lenses: execution rigor (backlog ownership, sprint ceremonies, delivery), technical depth (APIs, data infrastructure, integrations), growth/revenue (funnel, conversion, retention), compliance/regulatory, customer (discovery, research, NPS). State the lens you identified before making recommendations.",
        "- Distinguish between skills that are central to the role versus skills that describe the domain the PM will operate in. Domain-specific bullets should only rank above execution bullets when the role is explicitly hiring for that domain as a core competency, not merely operating within it.",
        "- Show only the bullets that should be kept — do not list or mention bullets that are being removed.",
        "- If a bullet could be strengthened with a slight tweak, rewrite it with the change applied, then add a sub-bullet (indented, starting with 'Change:') that briefly explains what was changed and why.",
        "- Never fabricate experience I have not had. Do not add new bullets to the final resume output without my explicit approval — instead, flag the gap and suggest what kind of bullet I could write if I have relevant experience, then ask me to confirm before including it.",
        "- If a bullet could benefit from a quantifiable metric, flag it.",
        "- Order bullets within each role by impact, highest first.",
        "- Each bullet must be a single sentence.",
        "- Give guidance on which skills to keep and which to remove.",
        "- Provide the suggested skills together, separated by | dividers.",
        "- Provide a recommended Professional Summary for this specific role.",
        "- Flag whether the resume would pass ATS, and note any issues.",
        "- Do not mirror the JD language too closely — avoid copy-pasting phrases from the job description.",
        "- Ensure language reflects the work of a PM, not an engineer.",
        "- Avoid reusing leading verbs and repeating terminology across bullets.",
        "- Do not use em dashes (—), en dashes (–), or double hyphens (--) anywhere in the output. These are hallmarks of LLM-generated writing and must be avoided entirely.",
        "- On first mention of a point-of-sale system in the resume, write out 'point-of-sale' in full. Use 'POS' for all subsequent mentions.",
        "- Bullet count targets by role:",
    ] + role_limits + [
        "",
        "# Bullet Audit Checklist",
        "Before producing any bullet in the final output, run this checklist against every bullet individually. Do not skip any step. Do not produce output until all steps are complete for all bullets.",
        "",
        "1. Does this bullet open with a leading verb used by any other bullet in the same role? If yes, replace the verb.",
        "2. Does the language describe what an engineer built, or what a PM owned and drove? If it reads as engineering work, reframe it around PM actions: defined, prioritized, shaped, launched, partnered, directed.",
        "3. Can any phrase be cut without losing meaning? If yes, cut it.",
        "4. If any of steps 1 through 3 resulted in a change, rewrite the bullet and add an indented sub-bullet starting with 'Change:' that states exactly what was changed and why.",
        "5. If no changes were needed, output the bullet as-is with no sub-bullet.",
        "",
        "Treat this checklist as a hard requirement. A bullet that passes through unreviewed is a failure condition, not a default.",
        "",
        "# Recommended Summary",
        f"Resume matcher suggested variant: {recommended_summary or 'general'}",
        "",
        summary_text.strip() if summary_text else "(no summary matched — write one based on the job description)",
        "",
        "# Master Resume — Full Bullet List by Role",
    ]

    if roles:
        for role in roles:
            lines.append("")
            lines.append(f"## {role['title']} at {role['company']}  (keep {role['max_bullets']} bullets max)")
            for bid in role["bullet_ids"]:
                text = bullets_by_id.get(bid)
                if text:
                    lines.append(f"- {text}")
    else:
        # Fallback: flat list if no roles section defined
        lines.append("")
        for bid, text in bullets_by_id.items():
            lines.append(f"- {text}")

    if matched_bullets:
        lines += [
            "",
            "# Resume Matcher — Top Recommended Bullets for This Role",
            "The tool flagged these as most relevant based on job description themes. Use as a starting point.",
            "",
        ]
        for i, text in enumerate(matched_bullets, 1):
            lines.append(f"{i}. {text}")

    return "\n".join(lines)


@app.route("/job/<int:job_id>/tailor")
def tailor(job_id: int):
    with _conn() as conn:
        row = db.get_job_by_id(conn, job_id)
    if row is None:
        abort(404)

    skills_bank: dict = app.config.get("SKILLS_BANK", {})
    prompt = _build_tailor_prompt(row, skills_bank)

    return render_template(
        "tailor.html.j2",
        job=row,
        prompt=prompt,
    )


@app.route("/tailor/adhoc", methods=["GET", "POST"])
def tailor_adhoc():
    skills_bank: dict = app.config.get("SKILLS_BANK", {})

    if request.method == "GET":
        return render_template("tailor_adhoc.html.j2")

    title = request.form.get("title", "").strip()
    company = request.form.get("company", "").strip()
    job_url = request.form.get("url", "").strip() or f"manual://{uuid.uuid4()}"
    description = request.form.get("description", "").strip()

    # Load profile for scoring
    profile: dict = {}
    if _PROFILE_PATH.exists():
        with open(_PROFILE_PATH) as f:
            profile = yaml.safe_load(f) or {}

    job_obj = db.Job(
        url=job_url,
        title=title,
        company=company,
        location="",
        site="manual",
        is_remote=False,
        description=description,
        date_posted=date.today().isoformat(),
    )

    db_path: Path = app.config["DB_PATH"]
    with db.connect(db_path) as conn:
        db.upsert_job(conn, job_obj)
        row = db.get_job_by_url(conn, job_url)
        job_id = row["id"]

        score_result = scorer.score_job(row, profile, skills_bank)
        matched_ids, summary_variant = resume_matcher.match_job(description, skills_bank)
        highlights = resume_matcher.extract_jd_highlights(description, skills_bank)
        db.update_scores(conn, job_id, score_result.score, score_result.rationale, summary_variant, matched_ids, highlights)

        industry = industry_classifier.classify(title, description or "")
        conn.execute("UPDATE jobs SET industry = ? WHERE id = ?", (industry, job_id))

    return redirect(url_for("tailor", job_id=job_id))


def _build_interview_prompt(job_row, skills_bank: dict) -> str:
    """Assemble a ready-to-paste Claude.ai interview prep prompt."""
    import json

    title = job_row["title"] or ""
    company = job_row["company"] or ""
    url = job_row["url"] or ""
    description = job_row["description"] or ""

    bullets_by_id = {b["id"]: b["text"] for b in skills_bank.get("bullets", [])}
    raw_ids = job_row["matched_bullet_ids"]
    matched_ids = json.loads(raw_ids) if raw_ids else []
    matched_bullets = [bullets_by_id[bid] for bid in matched_ids if bid in bullets_by_id]

    lines = [
        "# Context",
        f"- I'm a Senior Product Manager interviewing for the role below.",
        f"- You are a senior hiring manager at {company} who has reviewed my resume and is preparing to interview me.",
        f"- Use the job description and my resume bullets below to generate targeted interview prep materials.",
        f"- Search the internet for recent information about {company}: their products, business model, recent news, funding, leadership, and any known challenges or strategic priorities. Use this context to make the questions and talking points more specific and relevant.",
        "",
        "# Role",
        f"{title} at {company}",
        f"{url}",
        "",
        "# Job Description",
        description,
        "",
        "# What I Need",
        "1. **Likely Interview Questions** — Generate 10-15 questions you'd expect from this company for this role.",
        "   - Include a mix of: behavioral (STAR-format), situational, technical/PM craft, and culture/values questions.",
        "   - Weight questions toward the skills and themes most emphasized in the job description.",
        "",
        "2. **Talking Points per Question** — For each question, provide 2-3 bullet talking points I should hit.",
        "   - Ground them in the resume bullets below where possible.",
        "   - Flag where I should lead with a specific metric or outcome.",
        "",
        "3. **STAR Story Mapping** — For the top 5 behavioral questions, identify which of my resume bullets",
        "   best maps to a STAR story and explain the connection.",
        "",
        "4. **Questions to Ask Them** — Suggest 5 strong questions I should ask the interviewer,",
        "   tailored to this role and company.",
        "",
        "5. **Watch-outs** — Flag any areas in the JD where my resume has an obvious gap,",
        "   and suggest how I might address or reframe it.",
        "",
        "# My Resume — Top Matched Bullets for This Role",
        "These are the bullets most relevant to this job description:",
        "",
    ]

    for i, text in enumerate(matched_bullets, 1):
        lines.append(f"{i}. {text}")

    return "\n".join(lines)


@app.route("/job/<int:job_id>/interview")
def interview(job_id: int):
    with _conn() as conn:
        row = db.get_job_by_id(conn, job_id)
    if row is None:
        abort(404)

    skills_bank: dict = app.config.get("SKILLS_BANK", {})
    prompt = _build_interview_prompt(row, skills_bank)

    return render_template(
        "tailor.html.j2",
        job=row,
        prompt=prompt,
        prompt_label="Interview prep",
    )


@app.route("/interview/adhoc", methods=["GET", "POST"])
def interview_adhoc():
    skills_bank: dict = app.config.get("SKILLS_BANK", {})

    if request.method == "GET":
        return render_template("tailor_adhoc.html.j2", mode="interview",
                               page_title="Interview prep — paste a job")

    title = request.form.get("title", "").strip()
    company = request.form.get("company", "").strip()
    job_url = request.form.get("url", "").strip() or f"manual://{uuid.uuid4()}"
    description = request.form.get("description", "").strip()

    profile: dict = {}
    if _PROFILE_PATH.exists():
        with open(_PROFILE_PATH) as f:
            profile = yaml.safe_load(f) or {}

    job_obj = db.Job(
        url=job_url,
        title=title,
        company=company,
        location="",
        site="manual",
        is_remote=False,
        description=description,
        date_posted=date.today().isoformat(),
    )

    db_path: Path = app.config["DB_PATH"]
    with db.connect(db_path) as conn:
        db.upsert_job(conn, job_obj)
        row = db.get_job_by_url(conn, job_url)
        job_id = row["id"]

        score_result = scorer.score_job(row, profile, skills_bank)
        matched_ids, summary_variant = resume_matcher.match_job(description, skills_bank)
        highlights = resume_matcher.extract_jd_highlights(description, skills_bank)
        db.update_scores(conn, job_id, score_result.score, score_result.rationale, summary_variant, matched_ids, highlights)

        industry = industry_classifier.classify(title, description or "")
        conn.execute("UPDATE jobs SET industry = ? WHERE id = ?", (industry, job_id))

    return redirect(url_for("interview", job_id=job_id))


@app.route("/connections", methods=["GET"])
def connections_page():
    with _conn() as conn:
        conns = db.get_connections(conn)
    return render_template("connections.html.j2", connections=conns)


@app.route("/connections", methods=["POST"])
def connections_add():
    name     = request.form.get("name", "").strip()
    company  = request.form.get("company", "").strip()
    position = request.form.get("position", "").strip() or None
    if not name or not company:
        abort(400)
    with _conn() as conn:
        db.add_connection(conn, name, company, position)
    return redirect(url_for("connections_page"))


@app.route("/connections/<int:conn_id>/delete", methods=["POST"])
def connections_delete(conn_id: int):
    with _conn() as conn:
        db.delete_connection(conn, conn_id)
    return redirect(url_for("connections_page"))


@app.route("/job/<int:job_id>/status", methods=["POST"])
def set_status(job_id: int):
    new_status = request.form.get("status", "")
    if new_status == "reset":
        resolved = None
    elif new_status in ("applied", "dismissed"):
        resolved = new_status
    else:
        abort(400)

    with _conn() as conn:
        row = db.get_job_by_id(conn, job_id)
        if row is None:
            abort(404)
        db.update_status(conn, job_id, resolved)

    if resolved == "applied":
        _push_to_job_search(job_id, row)
    elif resolved is None:
        _delete_from_job_search(job_id, row)

    return redirect(url_for("index"))
