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
    ranker, resume_matcher, scorer, scraper, tangerine, watchlist,
)
from job_puller.db import get_runs
from job_puller.reporter import _build_bullets_map

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
_PROFILE_PATH = Path(__file__).parent.parent / "profile" / "profile.yaml"

app = Flask(__name__, template_folder=str(_TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Jinja2 filters
# ---------------------------------------------------------------------------

@app.template_filter("format_isotime")
def _format_isotime(iso_str: str) -> str:
    """Convert ISO datetime string like '2026-06-19T12:18:23+00:00' to 'June 19, 2026'."""
    if not iso_str:
        return ""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        return iso_str[:10]


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
        "job_puller.watchlist", "job_puller.scorer", "job_puller.resume_matcher",
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

        # Watchlist (Lever/Ashby from connections)
        _put("stage", {"stage": "watchlist", "label": "Scraping Watchlist…"})
        wl_jobs = watchlist.scrape(config)
        jobs_fetched += len(wl_jobs)
        with db.connect(db_path) as conn:
            for job in wl_jobs:
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
    view = request.args.get("view", "runs")
    if view not in ("runs", "saved", "manual", "applied"):
        view = "runs"
    exclude_kw = _exclude_keywords()
    run_id_param = request.args.get("run", type=int)

    with _conn() as conn:
        runs = get_runs(conn)
        stats = db.get_run_stats(conn)
        all_connections = db.get_connections(conn)
        saved_count   = conn.execute("SELECT COUNT(*) FROM jobs WHERE is_saved = 1").fetchone()[0]
        manual_count  = conn.execute("SELECT COUNT(*) FROM jobs WHERE site = 'manual' AND status IS NULL").fetchone()[0]
        applied_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'applied'").fetchone()[0]

        if view == "saved":
            jobs = ranker.get_saved_jobs(conn)
            top_jobs = dismissed_jobs = []
        elif view == "manual":
            jobs = ranker.get_manual_jobs(conn)
            top_jobs = dismissed_jobs = []
        elif view == "applied":
            jobs = ranker.get_applied_jobs(conn)
            top_jobs = dismissed_jobs = []
        else:
            top_jobs = ranker.get_top_jobs(
                conn, app.config.get("TOP_N", 20), exclude_kw, run_id=run_id_param
            )
            dismissed_jobs = db.get_dismissed_jobs(conn)
            jobs = []

    connections_by_company: dict[str, list[str]] = {}
    for c in all_connections:
        key = (c["company"] or "").strip().lower()
        if key:
            connections_by_company.setdefault(key, []).append(c["name"])

    skills_bank: dict = app.config.get("SKILLS_BANK", {})

    return render_template(
        "dashboard.html.j2",
        view=view,
        top_jobs=top_jobs,
        jobs=jobs,
        dismissed_jobs=dismissed_jobs,
        stats=stats,
        skills_bank_bullets=_build_bullets_map(skills_bank),
        is_server=True,
        generated_at=None,
        runs=runs,
        selected_run_id=run_id_param,
        connections_by_company=connections_by_company,
        saved_count=saved_count,
        manual_count=manual_count,
        applied_count=applied_count,
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

    # Load user-defined prompt context from profile.yaml
    prompt_ctx: dict = {}
    if _PROFILE_PATH.exists():
        with open(_PROFILE_PATH) as _f:
            _profile = yaml.safe_load(_f) or {}
        prompt_ctx = _profile.get("prompt_context", {})
    role_description = prompt_ctx.get("role_description", "a job applicant")
    tailor_guidance: list[str] = prompt_ctx.get("tailor_guidance", [])

    lines = [
        f"I would like you to help me tailor my resume for a {title} role at {company}.",
        f"I'll share the full job description and my resume bullets below. Please review them and produce a tailored version of my resume optimized for this specific role at {company}.",
        "",
        "# Context",
        f"- I'm {role_description} applying for the role below.",
        f"- Below is my full master resume, organized by role with every bullet point.",
        f"- You are a hiring manager at {company} reviewing my application for this specific role.",
        f"- You are well-versed in best practices for resume writing for this type of role.",
        "",
        "# Role",
        f"{title} at {company}",
        f"{url}",
        "",
        "# Job Description",
        description,
        "",
        "# Required: Evaluation Lens Identification",
        "Before reviewing any bullets or producing any output, identify and state the primary evaluation lens this role uses to assess candidates. Complete this block first. Your entire subsequent analysis — bullet selection, ranking, summary choice, and misframe detection — must be filtered through the lens you state here. Do not skip or defer this step.",
        "",
        "Evaluation Lens: [state the primary lens]",
        "Why: [one sentence explaining which signals in the JD led you to this conclusion]",
        "Secondary lens (if any): [or 'none']",
        "",
        "Common lenses: execution and delivery rigor | technical depth | growth and revenue impact | customer discovery and research | AI fluency and workflow building | GTM and storytelling | compliance and regulatory depth",
        "",
        "# Specific Guidance",
        "- Before recommending bullets, identify the primary evaluation lens this role uses to assess candidates. Do not default to surface-level keyword matching. State the lens you identified before making recommendations.",
        "- Distinguish between skills that are central to the role versus skills that describe the domain the candidate will operate in. Domain-specific bullets should only rank above execution bullets when the role is explicitly hiring for that domain as a core competency, not merely operating within it.",
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
        "- Avoid reusing leading verbs and repeating terminology across bullets.",
        "- Do not use em dashes (—), en dashes (–), or double hyphens (--) anywhere in the output. These are hallmarks of LLM-generated writing and must be avoided entirely.",
        "- Bullet count targets by role:",
    ] + role_limits + (
        ["", "# Additional Guidance"] + [f"- {g}" if not g.startswith("-") else g for g in tailor_guidance]
        if tailor_guidance else []
    ) + [
        "",
        "# Bullet Audit Checklist",
        "Before producing any bullet in the final output, run this checklist against every bullet individually. Do not skip any step. Do not produce output until all steps are complete for all bullets.",
        "",
        "1. Does this bullet open with a leading verb used by any other bullet in the same role? If yes, replace the verb.",
        "2. Is the language outcome-focused and action-driven? Can it be made more concise without losing meaning? If yes, rewrite it.",
        "3. Can any phrase be cut without losing meaning? If yes, cut it.",
        "4. If any of steps 1 through 3 resulted in a change, rewrite the bullet and add an indented sub-bullet starting with 'Change:' that states exactly what was changed and why.",
        "5. If no changes were needed, output the bullet exactly as written with no sub-bullet, no annotation, and no commentary of any kind. Do not write 'Change: No changes needed', 'Change: Passes all steps', or any variation. Silence is the correct and complete output for an unchanged bullet.",
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
            "WARNING: These bullets were ranked by keyword and theme overlap only. This ranking does NOT reflect the evaluation lens and may elevate domain-context bullets above execution bullets. Do not treat this list as authoritative.",
            "",
            "Before using any bullet below, classify it silently as strong fit, partial fit, or potential misframe — do not include these labels in your output. For any bullet you classify as potential misframe, surface that flag explicitly: state why the framing is a mismatch for this role (not just this domain) and whether it should be reframed or omitted. A bullet with strong numbers that frames the candidate as the wrong type of PM must be flagged, not surfaced silently.",
            "",
            "If these bullets do not fully serve the evaluation lens you identified above, select additional or replacement bullets from the Master Resume section — do not limit yourself to this list.",
            "",
        ]
        for i, text in enumerate(matched_bullets, 1):
            lines.append(f"{i}. {text}")

    lines += [
        "",
        "# Human Voice Review — Required Before Presenting Output",
        "",
        "Before presenting any output, scan every bullet and summary sentence against this checklist.",
        "If you rewrite anything, note it explicitly: show the original and the revised version so the candidate can review the change.",
        "",
        "**Word-level — hard ban (replace any instance found):**",
        "leverage/leveraged, utilize/utilized, spearhead/spearheaded, facilitate (catch-all), streamline/streamlined,",
        "enhance/enhancing, enable/enabling, empower/empowering, foster/fostering, elevate, amplify, augment,",
        "unlock, unleash, innovative/innovation, cutting-edge, groundbreaking, transformative/transformation,",
        "dynamic, holistic/holistically, robust (generic), scalable (generic), seamless, pivotal, paramount,",
        "impactful, vital/essential/crucial/critical (as filler), significant/significantly (vague),",
        "cross-functional, stakeholder alignment, data-driven (generic), customer-centric, synergy,",
        "thought leadership, best practices, deep dive, actionable insights, paradigm, landscape (metaphorical),",
        "realm, game-changer, moving forward, going forward, in order to, it is worth noting, as such,",
        "fundamentally/ultimately (as filler), adept, cognizant, nuanced (overused), tapestry, kaleidoscope,",
        "treasure trove, linchpin, foray, drive/drove/driven (generic PM verb), various/numerous/vast (vague quantity)",
        "",
        "**Word-level — previously required (confirm compliance):**",
        "- No em dashes (—), en dashes (–), or double hyphens (--)",
        "- No 'not X, but Y' or 'not just X, but Y' constructions",
        "",
        "**Structure check:**",
        "- No bullet follows the skeleton: [Verb] [tool/method] to [outcome], resulting in [N]%",
        "- No two bullets across any role in the resume open with the same exact word — 'Delivered X' and 'Delivered Y' in different roles is a collision; 'Delivered X' and 'Designed Y' is not, even though both are past-tense action verbs",
        "- No abstract descriptor leads the concrete claim ('impactful,' 'scalable,' 'robust' before the fact)",
        "- No vague partnership opener ('Collaborated with,' 'Partnered with') unless no specific alternative exists",
        "",
        "**Voice check:**",
        "- Every bullet is specific enough that it could only describe this candidate — not any PM anywhere",
        "- The summary describes a specific person's work, not a generic PM profile",
        "",
        "# Cross-Role Redundancy Check — Required Before Presenting Output",
        "",
        "After finalizing all bullet selections, scan the selected bullets across every role in the resume for substantially similar claims.",
        "Two bullets are substantially similar when a reader would feel they are hearing the same story twice — same core action, same domain, and same type of outcome.",
        "Shared themes alone (e.g., two compliance bullets, two data bullets) are not enough; the claims must be substantively overlapping.",
        "",
        "If you find any substantially similar pairs:",
        "- Name the two bullets and which roles they come from",
        "- State in one sentence why they overlap",
        "- Do NOT cut either bullet — flag them and let the candidate decide which to keep, reframe, or cut",
        "",
        "If no overlapping pairs are found, skip this section silently.",
        "",
        "Examples of what counts as overlap:",
        "- Two bullets that both claim to have reduced due diligence time for financial institutions",
        "- Two bullets that both describe building self-service tools that reduced support burden",
        "- Two bullets from different roles that cite the same integration or product by name and make the same claim about it",
        "",
        "Examples of what does NOT count as overlap:",
        "- Two compliance bullets that address different regulatory frameworks or different customer outcomes",
        "- Two data pipeline bullets that describe distinct products or different layers of the stack",
        "- A bullet about building a feature and a separate bullet about the business impact of that same feature",
        "",
        "---",
        "BEGIN PROMPT FEEDBACK — OUTPUT THIS SECTION",
        "# PROMPT FEEDBACK SECTION",
        "You are now producing this section — it is not an instruction to follow during resume tailoring.",
        "After outputting the tailored resume above, append this section below the '---' delimiter.",
        "Do not include this feedback in the resume itself.",
        "",
        "Critique ONLY the prompt's structure and instructions — not the candidate's qualifications or resume quality.",
        "",
        "Address each:",
        "1. Missing instructions: What did you have to infer that should have been explicit?",
        "2. Conflicting instructions: What guidance pulled in opposite directions?",
        "3. Assumptions: What did this prompt assume you knew about the experience format?",
        "4. Over/under-specification: What was too vague or too rigid?",
        "5. Edge cases or failure modes: What scenarios produce wrong or broken output?",
        "6. Top one-line fix: The single change that would most improve this prompt.",
        "",
        "Keep each to 1-2 sentences. Be direct and specific. Avoid vague praise.",
    ]

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
    """Assemble a ready-to-paste interview prep prompt."""
    import json

    title = job_row["title"] or ""
    company = job_row["company"] or ""
    url = job_row["url"] or ""
    description = job_row["description"] or ""

    bullets_by_id = {b["id"]: b["text"] for b in skills_bank.get("bullets", [])}
    roles = skills_bank.get("roles", [])
    raw_ids = job_row["matched_bullet_ids"]
    matched_ids = json.loads(raw_ids) if raw_ids else []
    matched_bullets = [bullets_by_id[bid] for bid in matched_ids if bid in bullets_by_id]

    # Load user-defined prompt context from profile.yaml
    prompt_ctx_iv: dict = {}
    if _PROFILE_PATH.exists():
        with open(_PROFILE_PATH) as _f:
            _profile_iv = yaml.safe_load(_f) or {}
        prompt_ctx_iv = _profile_iv.get("prompt_context", {})
    role_description_iv = prompt_ctx_iv.get("role_description", "a job applicant")
    interview_guidance: list[str] = prompt_ctx_iv.get("interview_guidance", [])

    lines = [
        f"I would like you to help me prepare for an interview for a {title} role at {company}.",
        f"I'll share the job description and my resume bullets below. Please use them to generate targeted interview prep materials for this specific role at {company}.",
        "",
        "# Context",
        f"- I'm {role_description_iv} interviewing for the role below.",
        f"- You are a senior hiring manager at {company} who has reviewed my resume and is preparing to interview me.",
        f"- Use the job description and my resume bullets below to generate targeted, specific prep materials — not generic PM interview advice.",
        f"- Search the internet for recent information about {company}: their products, business model, recent news, funding, leadership, and any known challenges or strategic priorities. Use this context to make questions and talking points specific to this company and moment.",
        "",
        "# Role",
        f"{title} at {company}",
        f"{url}",
        "",
        "# Job Description",
        description,
        "",
        "# Step 1 — Identify the Evaluation Lens (do this silently before generating anything)",
        "Before producing any output, read the job description and identify the 1-2 primary lenses this role selects for.",
        "Choose from:",
        "- Execution: roadmap delivery, sprint ownership, cross-functional coordination, shipping on time",
        "- Product sense: user empathy, market intuition, product strategy, design taste, problem framing",
        "- Data/metrics: KPI ownership, analytical rigor, metric diagnosis, north star definition",
        "- Growth/GTM: acquisition, activation, retention, revenue, pricing, conversion",
        "- Technical depth: platform/infra PM, API fluency, systems thinking, engineering partnership",
        "- Leadership/influence: staff/group PM, managing through ambiguity, influencing without authority",
        "",
        "State the 1-2 lenses at the top of your output, then use them to weight the question distribution throughout.",
        "A metrics-heavy role warrants 4-5 metrics questions. A product sense role warrants 4-5 design/strategy questions.",
        "Do not default to a generic mix — the weighting should be visibly different per role.",
        "",
        "# Step 2 — Interview Questions by Category",
        "Generate 12-16 questions covering all of the following categories.",
        "Weight the count per category to match the lenses identified above.",
        "",
        "For each question, output:",
        "  - The question text",
        "  - A [Category] tag: Behavioral | Product Sense | Metrics | Execution | Leadership | Company-Specific",
        "  - One sentence: what signal is the interviewer actually testing for?",
        "",
        "Category targets (adjust count based on lens weighting):",
        "- **Behavioral (STAR)** [3-4]: Tests ownership, resilience, cross-functional influence, delivery under ambiguity.",
        "  Focus on situations where the candidate had to decide, drive, or course-correct with incomplete information.",
        "- **Product Sense / Design** [2-4, lens-weighted]: Tests user empathy, prioritization judgment, strategy clarity.",
        "  Questions like: improve a product, define a north star metric, launch a new feature for an unfamiliar user.",
        "  Good answers state the target user before any solution.",
        "- **Metrics / Analytical** [2-4, lens-weighted]: Tests KPI fluency, metric ownership, and diagnostic reasoning.",
        "  Questions like: DAU dropped 15% week-over-week — walk me through your investigation.",
        "  Or: what metric would you use to measure success for X? What would make you concerned?",
        "- **Execution / Prioritization** [2-3]: Tests trade-off reasoning, roadmap sequencing, scope decisions under constraint.",
        "  Questions like: you have 3 weeks before launch and engineering says feature X won't make it — what do you do?",
        "- **Leadership / Influence** [1-2, if Senior/Staff]: Tests stakeholder management and influence without authority.",
        "  Questions like: tell me about a time engineering pushed back on your roadmap and you had to get alignment.",
        "- **Company / Role-Specific** [1-2]: Questions only someone who researched this company could ask or answer well.",
        "  Ground these in your company research — specific product decisions, competitive dynamics, or recent news.",
        "",
        "# Step 3 — STAR Story Mapping",
        "For the top 5 behavioral questions, build an answer scaffold using my resume bullets below.",
        "",
        "For each:",
        "1. Identify the resume bullet that best anchors this story (cite it by number from the list below)",
        "2. Scaffold the answer in four beats:",
        "   - Situation: what was the context, team size, constraints, scale?",
        "   - Task: what was the specific task or goal I was responsible for?",
        "   - Action: what specific decisions or moves did *I* make (not 'we')?",
        "   - Result: what was the measurable outcome, before vs. after?",
        "3. If the anchoring bullet lacks a quantified result, flag it explicitly:",
        "   'Rehearsal gap: bullet #N has no metric — you need to add a number verbally before this interview,",
        "   or this answer will land soft. Candidate should think: what was the baseline? what changed? by how much?'",
        "",
        "# Step 4 — Resume Bullet Fit Assessment",
        "Before using any bullet in talking points or story mapping, silently classify it as:",
        "strong fit | partial fit | potential misframe",
        "",
        "For any bullet classified as potential misframe:",
        "- State why it risks misalignment with this role's primary lens",
        "- Example: 'This bullet frames the candidate as an infrastructure PM — if the role selects primarily for",
        "  product sense/growth, leading with it risks positioning you as the wrong archetype. Reframe around",
        "  the user outcome or business result, not the technical execution.'",
        "- Do not silently drop misframed bullets — surface the flag so the candidate can decide.",
        "",
        "# Step 5 — Questions to Ask Them",
        "Suggest exactly 5 questions I should ask the interviewer. Use this structure:",
        "",
        "- 1-2 questions about product/strategy: probe the team's conviction about where the product is going.",
        "  Good questions reveal whether there is a real point of view or just roadmap theater.",
        "- 1 question about team dynamics: PM-to-eng ratio, how decisions get made, what 'good' looks like here.",
        "- 1 question about success definition: what does winning look like in 6 months for this specific role?",
        "- 1 question about the hard thing: what is the biggest challenge the person in this role will face?",
        "",
        "Instruction: questions should reveal information genuinely useful for evaluating the role.",
        "Avoid anything answerable from the company website or LinkedIn. Avoid questions that just signal enthusiasm.",
        "",
        "# Step 6 — Watch-outs and Gap Classification",
        "Review the JD against my resume bullets. For every meaningful gap, classify it into one of three tiers:",
        "",
        "- **Dealbreaker gap**: the JD treats this as a hard requirement and my resume has no adjacent experience.",
        "  Flag explicitly: 'This gap needs to be addressed directly in the interview or the process may not advance.'",
        "- **Coachable gap**: the JD mentions this but my resume has adjacent or transferable experience.",
        "  Suggest a specific reframe that bridges my background to the requirement.",
        "- **Non-issue**: mentioned in the JD but not emphasized; my overall profile is strong enough to carry past it.",
        "  Note it briefly and move on — do not over-index on non-issues.",
        "",
        "Do not list every minor keyword miss. Focus on gaps that could materially affect the interview outcome.",
        "",
        "# Step 7 — Universal Fit Questions (Asked Every Interview)",
        "Include answer prep for these evergreen questions that every interviewer asks",
        "regardless of role or level. They test motivation, self-awareness, and commitment.",
        "Do not skip them.",
        "",
        "1. \"Why do you want to work here?\" (and variants: \"What interests you about this role?\",",
        "   \"Why are you leaving your current position?\")",
        "",
        "   Answer framework — three beats:",
        "   (a) MOTIVATION: State what the candidate is seeking next. Prefer pull over push.",
        "       For \"why are you leaving\": acknowledge briefly, immediately pivot to pull.",
        "   (b) SPECIFIC SIGNAL: Cite one concrete thing from the JD or company research —",
        "       a stated challenge, strategic priority, or product direction — as evidence",
        "       that the answer is prepared, not generic.",
        "   (c) CONTRIBUTION: \"I know I can help you solve [CHALLENGE]\", where [CHALLENGE]",
        "       comes from the intersection of the JD's hardest problem and the candidate's",
        "       strongest relevant experience.",
        "",
        "   Never: complain about current employer, cite salary as primary reason, or frame",
        "   departure as running-from rather than running-to.",
        "",
        "2. \"Where do you see yourself in N years?\"",
        "",
        "   Convert the trap into a commitment signal:",
        "   (a) \"I see myself here.\" — directly answers the question and signals intent.",
        "   (b) \"Becoming an expert in [AREA] and a valuable part of this team.\" — [AREA]",
        "       must be a GROWTH direction from the JD, not something already mastered.",
        "       If every matching area is already mastered, pick the one with the most upside.",
        "   (c) \"This role aligns with where I want to go because [SPECIFIC THING].\" — one",
        "       concrete alignment between the job's trajectory and the candidate's direction.",
        "",
    ] + (
        ["# Additional Guidance"] + [f"- {g}" if not g.startswith("-") else g for g in interview_guidance] + [""]
        if interview_guidance else []
    ) + [
        "# Master Resume — Full Bullet List by Role",
        "",
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
        lines.append("")
        for bid, text in bullets_by_id.items():
            lines.append(f"- {text}")

    lines += [
        "",
        "# My Resume — Top Matched Bullets for This Role",
        "These are the bullets most relevant to this job description.",
        "Use these as the primary source for talking points, STAR stories, and fit assessment.",
        "They are ranked by keyword overlap — use judgment about which are actually strong fits for this role's lens.",
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
    imported = request.args.get("imported", type=int)
    skipped  = request.args.get("skipped",  type=int)
    with _conn() as conn:
        conns = db.get_connections(conn)
    return render_template("connections.html.j2", connections=conns,
                           imported=imported, skipped=skipped)


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


@app.route("/connections/import", methods=["POST"])
def connections_import():
    import csv, io
    f = request.files.get("csv_file")
    if not f or not f.filename:
        abort(400)

    stream = io.TextIOWrapper(f.stream, encoding="utf-8-sig")  # handles LinkedIn BOM
    reader = csv.DictReader(stream)

    with _conn() as conn:
        existing = {
            (r["name"].strip().lower(), r["company"].strip().lower())
            for r in db.get_connections(conn)
        }

    imported, skipped = 0, 0
    rows_to_add = []

    for row in reader:
        first    = (row.get("First Name") or "").strip()
        last     = (row.get("Last Name") or "").strip()
        name     = f"{first} {last}".strip()
        company  = (row.get("Company") or "").strip()
        position = (row.get("Position") or "").strip() or None

        if not name or not company:
            skipped += 1
            continue

        key = (name.lower(), company.lower())
        if key in existing:
            skipped += 1
            continue

        existing.add(key)
        rows_to_add.append((name, company, position))

    with _conn() as conn:
        for name, company, position in rows_to_add:
            db.add_connection(conn, name, company, position, source="linkedin_csv")
            imported += 1

    return redirect(url_for("connections_page", imported=imported, skipped=skipped))


@app.route("/connections/<int:conn_id>/delete", methods=["POST"])
def connections_delete(conn_id: int):
    with _conn() as conn:
        db.delete_connection(conn, conn_id)
    return redirect(url_for("connections_page"))


@app.route("/job/<int:job_id>/save", methods=["POST"])
def toggle_save(job_id: int):
    from flask import jsonify
    with _conn() as conn:
        is_saved = db.toggle_saved(conn, job_id)
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"is_saved": is_saved})
    return redirect(request.referrer or url_for("index"))


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
            conn.execute("UPDATE jobs SET is_saved = 0, applied_at = datetime('now') WHERE id = ?", (job_id,))
        elif resolved is None:
            conn.execute("UPDATE jobs SET applied_at = NULL WHERE id = ?", (job_id,))

    if resolved == "applied":
        _push_to_job_search(job_id, row)
    elif resolved is None:
        _delete_from_job_search(job_id, row)

    return redirect(url_for("index"))


if __name__ == "__main__":
    raise SystemExit("Use: job-puller serve")
