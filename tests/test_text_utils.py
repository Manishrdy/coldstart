import pytest

from coldstart.text_utils import filter_resume_for_llm, strip_markdown_json_fences


def test_strip_markdown_json_fences_with_json_tag():
    raw = '```json\n{"a": 1}\n```'
    assert strip_markdown_json_fences(raw) == '{"a": 1}'


def test_strip_markdown_json_fences_without_json_tag():
    raw = '```\n{"a": 1}\n```'
    assert strip_markdown_json_fences(raw) == '{"a": 1}'


def test_strip_markdown_json_fences_plain_json_unchanged():
    raw = '{"a": 1}'
    assert strip_markdown_json_fences(raw) == '{"a": 1}'


def test_strip_markdown_json_fences_strips_surrounding_whitespace():
    raw = '  \n{"a": 1}\n  '
    assert strip_markdown_json_fences(raw) == '{"a": 1}'


# --- filter_resume_for_llm --------------------------------------------------------

_SAMPLE_RESUME = """\
Jane Doe
Backend engineer who ships things.
jane@example.com 555-123-4567 Austin, TX github.com/janedoe linkedin.com/in/janedoe
WORK EXPERIENCE
Acme Corp
Senior Engineer Jan 2020 - Present
• Built a thing using Python and Postgres.
EDUCATION
State University
Bachelor's in Computer Science
PROJECT
Personal Site
github.com/janedoe/site
• Built a personal site with Next.js.
SKILLS
Languages: Python, Go
"""


def test_filter_resume_drops_name_and_contact_preamble():
    out = filter_resume_for_llm(_SAMPLE_RESUME)
    assert "Jane Doe" not in out
    assert "jane@example.com" not in out
    assert "555-123-4567" not in out
    assert "Austin" not in out
    assert "Backend engineer who ships things" not in out


def test_filter_resume_drops_education_section():
    out = filter_resume_for_llm(_SAMPLE_RESUME)
    assert "State University" not in out
    assert "Bachelor" not in out


def test_filter_resume_keeps_work_experience_project_and_skills():
    out = filter_resume_for_llm(_SAMPLE_RESUME)
    assert "Acme Corp" in out
    assert "Built a thing using Python and Postgres" in out
    assert "Personal Site" in out
    assert "Built a personal site with Next.js" in out
    assert "Languages: Python, Go" in out


def test_filter_resume_redacts_github_and_linkedin_links_anywhere():
    out = filter_resume_for_llm(_SAMPLE_RESUME)
    assert "github.com" not in out
    assert "linkedin.com" not in out
    assert "janedoe" not in out  # the username itself, not just the domain


def test_filter_resume_drops_unrecognized_sections():
    text = _SAMPLE_RESUME + "\nREFERENCES\nAvailable upon request.\n"
    out = filter_resume_for_llm(text)
    assert "Available upon request" not in out


def test_filter_resume_mixed_case_company_names_not_mistaken_for_headers():
    # A mixed-case company/product name must never be treated as a section
    # boundary — only ALL-CAPS standalone lines are.
    out = filter_resume_for_llm(_SAMPLE_RESUME)
    assert "Acme Corp" in out  # still attached to the WORK EXPERIENCE section


def test_filter_resume_case_insensitive_keyword_match():
    text = _SAMPLE_RESUME.replace("SKILLS", "TECHNICAL SKILLS")
    out = filter_resume_for_llm(text)
    assert "Languages: Python, Go" in out


def test_filter_resume_no_recognized_sections_raises():
    with pytest.raises(ValueError, match="no work-experience/project/skills"):
        filter_resume_for_llm("Jane Doe\nSome unstructured text with no headers at all.")
