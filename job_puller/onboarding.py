"""Onboarding wizard — YAML read/write, LLM prompt generation, upload validation."""

import json
import re
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# YAML readers
# ---------------------------------------------------------------------------

def read_profile(path: Path) -> dict[str, Any]:
    """Parse profile.yaml and return a structured dict."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def read_skills_bank(path: Path) -> dict[str, Any]:
    """Parse skills_bank.yaml and return a structured dict."""
    if not path.exists():
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Bullet ID generation
# ---------------------------------------------------------------------------

def assign_bullet_id(text: str, existing_ids: set[str] | None = None) -> str:
    """Generate a kebab-case ID from bullet text.

    Takes the first 3-4 significant words, lowercased and kebab-joined.
    Appends a numeric suffix if the ID already exists.
    """
    existing_ids = existing_ids or set()
    # Strip quotes and parentheticals, take first ~4 meaningful words
    cleaned = re.sub(r'[\(\)"\'"]', "", text)
    words = [w for w in cleaned.split() if len(w) > 2][:4]
    base = "-".join(w.lower().rstrip(".,;:!?") for w in words)
    if not base:
        base = "bullet"
    # Avoid duplication
    candidate = base
    suffix = 1
    while candidate in existing_ids:
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate


# ---------------------------------------------------------------------------
# YAML writers (Jinja2 templates for consistent formatting)
# ---------------------------------------------------------------------------

_PROFILE_TEMPLATE = """\
name: "{{ name }}"

target_titles:
{% for t in target_titles %}
  - "{{ t }}"
{% endfor %}

target_levels:
{% for l in target_levels %}
  - {{ l }}
{% endfor %}

years_experience: {{ years_experience }}

target_salary_min: {{ salary_min }}
target_salary_max: {{ salary_max }}

preferred_remote: {{ preferred_remote }}

preferred_locations:
{% for loc in preferred_locations %}
  - "{{ loc }}"
{% endfor %}

preferred_company_size:
{% for s in preferred_company_size %}
  - {{ s }}
{% endfor %}

avoid_keywords:
{% for kw in avoid_keywords %}
  - "{{ kw }}"
{% endfor %}

exclude_title_keywords:
{% for kw in exclude_title_keywords %}
  - "{{ kw }}"
{% endfor %}

prompt_context:
  role_description: "{{ role_description }}"
  tailor_guidance: []
  interview_guidance: []

industries:
{% for ind in industries %}
  - "{{ ind }}"
{% endfor %}
"""


def _e(template: str, data: dict) -> str:
    """Render a Jinja2-like template with the given data.

    Uses str.replace-based substitution since Jinja2 isn't needed for these
    simple templates and keeping it dependency-free avoids complexity.
    Only supports {{ var }} and {% for v in list %} ... {% endfor %} constructs.
    Does NOT support Jinja2 filters, conditionals, or nesting beyond one level.
    """
    result = template
    # Replace simple {{ var }} placeholders
    for key, val in data.items():
        if isinstance(val, str):
            result = result.replace("{{ " + key + " }}", val)
        elif isinstance(val, bool):
            result = result.replace("{{ " + key + " }}", str(val).lower())
        elif isinstance(val, (int, float)):
            result = result.replace("{{ " + key + " }}", str(val))

    # Handle {% for %} blocks — one at a time using search(), since each
    # substitution changes the string length and invalidates prior positions.
    for_match = re.compile(
        r"{% for (\w+) in (\w+) %}(.*?){% endfor %}",
        re.DOTALL,
    )
    while True:
        m = for_match.search(result)
        if not m:
            break
        var_name = m.group(1)
        list_name = m.group(2)
        body = m.group(3)
        items = data.get(list_name, [])
        replacement = ""
        for item in items:
            item_str = body
            if isinstance(item, str):
                item_str = item_str.replace("{{ " + var_name + " }}", item)
            elif isinstance(item, dict):
                for k, v in item.items():
                    if isinstance(v, str):
                        item_str = item_str.replace(
                            "{{ " + var_name + "." + k + " }}", v
                        )
                    elif isinstance(v, (int, float)):
                        item_str = item_str.replace(
                            "{{ " + var_name + "." + k + " }}", str(v)
                        )
                    elif isinstance(v, list):
                        item_str = item_str.replace(
                            "{{ " + var_name + "." + k + " }}", ", ".join(v)
                        )
            else:
                item_str = item_str.replace("{{ " + var_name + " }}", str(item))
            replacement += item_str
        result = result[: m.start()] + replacement + result[m.end() :]

    # Clean up any remaining unreplaced placeholders
    result = re.sub(r"\{\{.*?\}\}", "", result)
    return result


_SKILLS_TEMPLATE = """\
name: "{{ name }}"
email: "{{ email }}"
location: "{{ location }}"
linkedin: "{{ linkedin }}"

summary_variants:
{% for theme in themes %}
  {{ theme.label }}: >
    {{ theme.summary }}
{% endfor %}

education:
{% for edu in education %}
  - degree: "{{ edu.degree }}"
    school: "{{ edu.school }}"
    year: {{ edu.year }}
{% endfor %}

certifications:
{% for cert in certifications %}
  - "{{ cert }}"
{% endfor %}

bullets:
{% for role in roles %}
  # {{ role.company }} — {{ role.title }}
{% for bullet in role.bullets %}
  - id: {{ bullet.id }}
    text: "{{ bullet.text }}"
    themes: [{{ bullet.themes | join(', ') }}]
    strength: {{ bullet.strength }}
{% endfor %}
{% endfor %}
"""


def write_profile_yaml(path: Path, data: dict) -> None:
    """Write profile.yaml from structured data using template."""
    with open(path, "w") as f:
        f.write(_PROFILE_TEMPLATE)

    # Apply substitutions
    text = path.read_text()
    replacements = {
        "name": data.get("name", ""),
        "target_titles": data.get("target_titles", []),
        "target_levels": [str(l) for l in data.get("target_levels", [])],
        "years_experience": data.get("years_experience", 0),
        "salary_min": data.get("salary_min", 0),
        "salary_max": data.get("salary_max", 0),
        "preferred_remote": data.get("preferred_remote", True),
        "preferred_locations": data.get("preferred_locations", []),
        "preferred_company_size": data.get("preferred_company_size", []),
        "avoid_keywords": data.get("avoid_keywords", []),
        "exclude_title_keywords": data.get("exclude_title_keywords", []),
        "role_description": data.get("role_description", "a job applicant"),
        "industries": data.get("industries", []),
    }
    rendered = _e(text, replacements)
    # Clean up trailing whitespace on empty lines
    rendered = "\n".join(line.rstrip() for line in rendered.split("\n"))
    path.write_text(rendered)


def write_skills_bank_yaml(
    path: Path,
    name: str,
    email: str,
    location: str,
    linkedin: str,
    themes: list[dict],
    education: list[dict],
    certifications: list[str],
    roles: list[dict],
) -> None:
    """Write skills_bank.yaml from structured data using template."""
    # We'll use the _e() renderer but need to handle the more complex template
    # For now, build the YAML from structured data
    lines = []
    _q = lambda s: f'"{s}"'

    lines.append(f"name: {_q(name)}")
    lines.append(f"email: {_q(email)}")
    lines.append(f"location: {_q(location)}")
    lines.append(f"linkedin: {_q(linkedin)}")
    lines.append("")
    lines.append("summary_variants:")
    for theme in themes:
        lines.append(f"  {theme['label']}: >")
        lines.append(f"    {theme['summary']}")
    lines.append("")
    lines.append("education:")
    for edu in education:
        lines.append(f"  - degree: {_q(edu.get('degree', ''))}")
        lines.append(f"    school: {_q(edu.get('school', ''))}")
        lines.append(f"    year: {edu.get('year', '')}")
    lines.append("")
    if certifications:
        lines.append("certifications:")
        for cert in certifications:
            lines.append(f"  - {_q(cert)}")
    else:
        lines.append("certifications: []")
    lines.append("")
    lines.append("bullets:")
    for role in roles:
        company = role.get("company", "")
        title = role.get("title", "")
        lines.append(f"  # {company} — {title}")
        for bullet in role.get("bullets", []):
            themes_str = ", ".join(bullet.get("themes", []))
            lines.append(f"  - id: {bullet.get('id', '')}")
            lines.append(f"    text: {_q(bullet.get('text', ''))}")
            lines.append(f"    themes: [{themes_str}]")
            lines.append(f"    strength: {bullet.get('strength', 'medium')}")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n")


# ---------------------------------------------------------------------------
# LLM prompt generation
# ---------------------------------------------------------------------------

def _build_template_yaml(name: str, email: str, location: str, linkedin: str) -> str:
    """Build the template YAML that gets embedded in the LLM prompt."""
    lines = []
    _q = lambda s: f'"{s}"'

    lines.append(f"name: {_q(name)}")
    lines.append(f"email: {_q(email)}")
    lines.append(f"location: {_q(location)}")
    lines.append(f"linkedin: {_q(linkedin)}")
    lines.append("")
    lines.append("summary_variants:")
    lines.append("  # Replace 'theme-1' etc. with the actual themes you identify")
    lines.append("  # from the candidate's resume content.")
    lines.append("  theme-1: >")
    lines.append("    Write a 2-3 sentence summary here for the first identified theme.")
    lines.append("  theme-2: >")
    lines.append("    Write a 2-3 sentence summary here for the second identified theme.")
    lines.append("  theme-3: >")
    lines.append("    Write a 2-3 sentence summary here for the third identified theme.")
    lines.append("  # Add or remove theme sections as needed (max 7).")
    lines.append("")
    lines.append("education:")
    lines.append("  - degree: \"Bachelor of Science in ...\"")
    lines.append("    school: \"University Name\"")
    lines.append("    year: YYYY")
    lines.append("")
    lines.append("certifications:")
    lines.append("  - \"Certification Name\"")
    lines.append("")
    lines.append("bullets:")
    lines.append("  # Company Name — Job Title")
    lines.append("  - id: your-bullet-id")
    lines.append('    text: "Achieved X by doing Y, resulting in Z% improvement."')
    lines.append("    themes: [theme-1, theme-2]")
    lines.append("    strength: high")
    lines.append("  - id: another-bullet-id")
    lines.append('    text: "Led team to deliver project under budget by N%."')
    lines.append("    themes: [theme-2, theme-3]")
    lines.append("    strength: medium")
    lines.append("")

    return "\n".join(lines)


def generate_llm_prompt(name: str, email: str, location: str, linkedin: str) -> str:
    """Generate the full LLM prompt including embedded template YAML.

    The user copies this into their LLM of choice along with their resume files.
    """
    template_yaml = _build_template_yaml(name, email, location, linkedin)

    return f"""You are a professional resume analyst and career coach. A user has provided their resume documents below. Your job is to extract all relevant experience and structure it into a YAML file that will power a job-search tool.

## What to do

1. Read all uploaded resume documents carefully.
2. Identify each role/company the candidate has held, with dates.
3. For each role, write 4-8 bullet points describing the candidate's key achievements.
   - Each bullet should be concrete and quantified where possible.
   - Use strong, specific verbs.
   - Focus on outcomes and impact, not responsibilities.
4. **Classify the dominant themes** in the candidate's career. These might include:
   - data, technical, api, growth, customer, compliance, fintech, self-serve, analytics, leadership, startup, developer-platform
   - Or identify other themes that better fit this candidate's unique experience.
   - You should identify 3-7 themes total.
   - Each bullet should be tagged with 1-3 of these themes.
5. Assign a strength level to each bullet:
   - **high** — lead bullets, strongest achievements
   - **medium** — solid supporting evidence
   - **supporting** — context/filler, used only to reach 8-10 bullets
6. Write a 2-3 sentence summary variant for each identified theme.
7. Fill in education and certifications.
8. Output the complete YAML following the template structure below exactly.

## Output requirements

- Return ONLY valid YAML — no explanations, no markdown wrappers, no code fences. Do NOT wrap the YAML in ```yaml or ``` blocks.
- Bullet IDs should be short kebab-case identifiers derived from the bullet text.
- Each bullet must have: id, text, themes (list of 1-3), and strength.
- Summary variants must use the YAML block scalar format (>).
- Every string value must be double-quoted.
- Do not add any fields beyond what the template shows.

## Template

```yaml
{template_yaml}```
"""


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

ValidationResult = dict[str, Any]
"""Return shape: {"valid": bool, "errors": list[str], "data": dict|None}"""


def validate_upload(yaml_text: str, required_name: str = "") -> ValidationResult:
    """Validate an uploaded YAML file against the expected skills bank schema.

    Returns validation result with parsed data on success or error list on failure.
    """
    errors: list[str] = []

    # 1. Strip markdown code fences if present
    yaml_text = yaml_text.strip()
    if yaml_text.startswith("```"):
        # Remove opening fence (```yaml, ```yml, ```)
        first_newline = yaml_text.find("\n")
        if first_newline != -1:
            yaml_text = yaml_text[first_newline + 1:]
        # Remove closing fence if present
        close_marker = yaml_text.rfind("```")
        if close_marker != -1:
            yaml_text = yaml_text[:close_marker]
        yaml_text = yaml_text.strip()

    # 2. Parse YAML
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        return {"valid": False, "errors": [f"Invalid YAML: {e}"], "data": None}

    if not isinstance(data, dict):
        return {"valid": False, "errors": ["YAML file must contain a mapping (dictionary)."], "data": None}

    # 3. Required top-level fields
    if not data.get("name"):
        errors.append("Missing required field: name")
    if not data.get("email"):
        errors.append("Missing required field: email")

    # 3. Summary variants / themes
    variants = data.get("summary_variants", {})
    if not isinstance(variants, dict) or len(variants) == 0:
        errors.append("At least one summary variant (theme) is required under 'summary_variants'.")
    elif len(variants) > 7:
        errors.append(f"Maximum of 7 themes allowed, found {len(variants)}.")

    # 4. Bullets
    bullets = data.get("bullets", [])
    if not isinstance(bullets, list) or len(bullets) == 0:
        errors.append("At least one bullet is required under 'bullets'.")

    # Validate each bullet
    seen_ids: set[str] = set()
    valid_themes = set(variants.keys()) if isinstance(variants, dict) else set()
    for i, bullet in enumerate(bullets):
        if not isinstance(bullet, dict):
            errors.append(f"Bullet #{i+1}: must be a mapping (dictionary).")
            continue
        bid = bullet.get("id", "")
        if not bid:
            errors.append(f"Bullet #{i+1}: missing 'id'.")
        elif bid in seen_ids:
            errors.append(f"Bullet #{i+1}: duplicate id '{bid}'.")
        else:
            seen_ids.add(bid)
        if not bullet.get("text"):
            errors.append(f"Bullet #{i+1}: missing 'text'.")
        bthemes = bullet.get("themes", [])
        if isinstance(bthemes, list) and valid_themes:
            invalid = [t for t in bthemes if t not in valid_themes]
            if invalid:
                errors.append(f"Bullet #{i+1}: unknown themes {invalid}. Valid: {sorted(valid_themes)}")
        if bullet.get("strength") not in (None, "high", "medium", "supporting"):
            errors.append(f"Bullet #{i+1}: strength must be high/medium/supporting, got '{bullet.get('strength')}'.")

    # 5. Education
    education = data.get("education", [])
    if isinstance(education, list):
        for i, edu in enumerate(education):
            if isinstance(edu, dict):
                if not edu.get("degree"):
                    errors.append(f"Education #{i+1}: missing or empty 'degree'.")
                if not edu.get("school"):
                    errors.append(f"Education #{i+1}: missing or empty 'school'.")
    else:
        errors.append("'education' must be a list.")

    if errors:
        return {"valid": False, "errors": errors, "data": None}

    return {"valid": True, "errors": [], "data": data}


# ---------------------------------------------------------------------------
# Data extraction from parsed YAML
# ---------------------------------------------------------------------------

def extract_themes_from_upload(data: dict) -> list[dict]:
    """Extract themes (summary variants) from uploaded YAML data.

    Returns list of {"label": str, "summary": str}.
    """
    variants = data.get("summary_variants", {})
    themes = []
    for label, summary in variants.items():
        if isinstance(summary, str):
            themes.append({"label": label, "summary": summary.strip()})
        elif isinstance(summary, dict):
            # Handle case where YAML parser produces a dict from folded blocks
            summary_str = str(summary) if summary else ""
            themes.append({"label": label, "summary": summary_str.strip()})
    return themes


def extract_roles_from_upload(data: dict) -> list[dict]:
    """Extract roles with bullets from uploaded YAML data.

    Returns list of {"role_label": str, "company": str, "title": str, "bullets": [...]}

    Note: The flat bullet list in skills_bank.yaml doesn't have explicit
    role grouping by default. We look for comment-based grouping or
    infer roles from the bullet structure. For now, all bullets go into
    a single "Experience" bucket.
    """
    # Skills bank format: bullets are flat, with optional # Company — Title comments
    # We don't preserve comments through yaml.safe_load, so we'll return bullets
    # as a single group initially. The editor lets users organize later.
    raw = data.get("bullets", [])
    bullets = []
    for b in raw:
        if isinstance(b, dict):
            bullets.append({
                "id": b.get("id", ""),
                "text": b.get("text", ""),
                "themes": b.get("themes", []),
                "strength": b.get("strength", "medium"),
            })
    return [{
        "role_label": "experience",
        "company": "",
        "title": "",
        "bullets": bullets,
    }]


def extract_education_from_upload(data: dict) -> list[dict]:
    return data.get("education", []) or []


def extract_certifications_from_upload(data: dict) -> list[str]:
    certs = data.get("certifications", []) or []
    return [c for c in certs if isinstance(c, str)]


def make_profile_data_from_form(form: dict) -> dict:
    """Convert web form data into the profile.yaml data structure."""
    target_titles = _parse_list(form.get("target_titles", ""))
    target_levels = _parse_list(form.get("target_levels", ""))
    preferred_locations = _parse_list(form.get("preferred_locations", ""))
    preferred_company_size = _parse_list(form.get("preferred_company_size", ""))
    avoid_keywords = _parse_list(form.get("avoid_keywords", ""))
    exclude_title_keywords = _parse_list(form.get("exclude_title_keywords", ""))
    industries = _parse_list(form.get("industries", ""))

    return {
        "target_titles": target_titles,
        "target_levels": target_levels,
        "years_experience": _int_or(form.get("years_experience"), 0),
        "salary_min": _int_or(form.get("salary_min"), 0),
        "salary_max": _int_or(form.get("salary_max"), 0),
        "preferred_remote": form.get("preferred_remote") == "true",
        "preferred_locations": preferred_locations,
        "preferred_company_size": preferred_company_size,
        "avoid_keywords": avoid_keywords,
        "exclude_title_keywords": exclude_title_keywords,
        "role_description": form.get("role_description", ""),
        "industries": industries,
    }


def _parse_list(val: str) -> list[str]:
    """Parse a newline-separated list from a textarea."""
    if not val:
        return []
    return [line.strip() for line in val.strip().split("\n") if line.strip()]


def _int_or(val: str | int, default: int = 0) -> int:
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (ValueError, TypeError):
        return default