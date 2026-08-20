"""Load and render the operator-editable digest templates.

The email used to be built entirely in Python f-strings, which meant no
restyling without editing code. These templates live in `config/email/` and
are read from disk on **every** render, so an edit takes effect on the next
digest with no restart — and the dashboard's /preview/email route renders
them live while you work.

Two non-negotiables:

1. **A broken template must never cost a day's matches.** Any template error
   is logged, recorded, and falls back to the built-in layout in digest.py.
   The digest still goes out.
2. **Autoescaping is always on.** Company names and job titles come from
   third-party feeds and land in HTML; a template author must not be able to
   turn that into an injection by forgetting a filter.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateError

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "email"
HTML_TEMPLATE = "digest.html.j2"
TEXT_TEMPLATE = "digest.txt.j2"
THEME_FILE = "theme.json"


class TemplateProblem(Exception):
    """A template or theme file could not be loaded or rendered."""


def _long_date(value: date | datetime) -> str:
    # %-d is platform-specific; strip the leading zero by hand instead.
    return f"{value:%A, %B} {value.day}, {value:%Y}"


def _short_date(value: date | datetime) -> str:
    return f"{value:%b} {value.day}"


def _utc_stamp(value: datetime) -> str:
    return f"{value:%Y-%m-%d %H:%M}"


def _autoescape(template_name: str | None) -> bool:
    """Escape HTML templates, never text ones.

    Escaping everything leaks `&amp;` and `&#39;` into the text/plain part;
    escaping nothing would let a third-party company name inject markup into
    the HTML. It has to be per-template, and it is decided here rather than
    in the template so a template author cannot switch it off by accident."""
    return bool(template_name) and template_name.endswith((".html.j2", ".html", ".htm"))


def _build_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=_autoescape,
        # Read from disk every render: that is what makes edits take effect
        # without a restart. The templates are a few KB and a digest is sent
        # once a day, so there is nothing to gain from caching them.
        auto_reload=True,
        cache_size=0,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["long_date"] = _long_date
    env.filters["date"] = _short_date
    env.filters["utc_stamp"] = _utc_stamp
    return env


def load_theme() -> dict[str, Any]:
    """Colours, brand and limits, editable without touching the template."""
    path = TEMPLATE_DIR / THEME_FILE
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise TemplateProblem(f"{THEME_FILE} could not be read: {exc}") from exc


def template_version() -> str:
    """A token that changes whenever any template file changes.

    The preview page polls this and reloads itself, which is what makes
    editing feel live rather than a save-and-refresh loop."""
    parts = []
    for name in (HTML_TEMPLATE, TEXT_TEMPLATE, THEME_FILE):
        path = TEMPLATE_DIR / name
        parts.append(f"{name}:{path.stat().st_mtime_ns if path.exists() else 0}")
    return "|".join(parts)


def render(template_name: str, *, sections, run_date: date) -> str:
    """Render one template. Raises TemplateProblem on any failure."""
    theme = load_theme()
    try:
        template = _build_env().get_template(template_name)
        return template.render(
            sections=sections,
            run_date=run_date,
            theme=theme,
            brand=theme.get("brand", "Coldstart"),
            tagline=theme.get("tagline", "Daily match digest"),
        )
    except TemplateError as exc:
        raise TemplateProblem(f"{template_name}: {exc}") from exc
    except OSError as exc:
        raise TemplateProblem(f"{template_name} could not be read: {exc}") from exc
