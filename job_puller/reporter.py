"""Static HTML report generator — renders dashboard.html.j2 to reports/YYYY-MM-DD.html."""

import json
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"


def _build_bullets_map(skills_bank: dict) -> dict[str, str]:
    """Return {bullet_id: first 80 chars of bullet text} for teaser rendering."""
    return {
        b["id"]: b.get("text", "")[:80]
        for b in skills_bank.get("bullets", [])
    }


def generate_report(
    top_jobs: list,
    applied_jobs: list,
    stats: dict,
    skills_bank: dict,
    output_dir: Path,
) -> Path:
    """Render the static HTML digest and write to output_dir/YYYY-MM-DD.html."""
    output_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(loader=FileSystemLoader(str(_TEMPLATES_DIR)), autoescape=True)
    template = env.get_template("dashboard.html.j2")

    html = template.render(
        top_jobs=top_jobs,
        applied_jobs=applied_jobs,
        dismissed_jobs=[],  # never shown in static report
        stats=stats,
        skills_bank_bullets=_build_bullets_map(skills_bank),
        is_server=False,
        generated_at=date.today().isoformat(),
    )

    out_path = output_dir / f"{date.today().isoformat()}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
