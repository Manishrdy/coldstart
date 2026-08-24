import pytest

from coldstart.text_utils import (
    filter_resume_for_llm,
    strip_markdown_json_fences,
    trim_job_description_for_llm,
)


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


# --- trim_job_description_for_llm -------------------------------------------------

_SAMPLE_JD_BOLD = """\
Acme Corp is a fast-growing company building the future of widgets.

**About Us**
Founded in 2015, Acme has raised $50M from top investors and serves \
thousands of customers worldwide.

**Responsibilities**
- Design and build backend services in Python.
- Own the on-call rotation for the payments team.

**Qualifications**
- 5+ years of backend engineering experience.
- Strong knowledge of distributed systems.

**Benefits**
- Unlimited PTO and a 401k match.
- Free lunch every day.

**Equal Opportunity Employer**
Acme Corp is proud to be an equal opportunity employer and does not \
discriminate on the basis of race, gender, or any other protected class.
"""


def test_trim_jd_keeps_preamble():
    out = trim_job_description_for_llm(_SAMPLE_JD_BOLD)
    assert "fast-growing company building the future of widgets" in out


def test_trim_jd_drops_about_us_section():
    out = trim_job_description_for_llm(_SAMPLE_JD_BOLD)
    assert "raised $50M from top investors" not in out


def test_trim_jd_drops_benefits_section():
    out = trim_job_description_for_llm(_SAMPLE_JD_BOLD)
    assert "Unlimited PTO" not in out
    assert "Free lunch" not in out


def test_trim_jd_drops_eeo_section():
    out = trim_job_description_for_llm(_SAMPLE_JD_BOLD)
    assert "does not discriminate" not in out


def test_trim_jd_keeps_responsibilities_and_qualifications():
    out = trim_job_description_for_llm(_SAMPLE_JD_BOLD)
    assert "Design and build backend services in Python" in out
    assert "5+ years of backend engineering experience" in out


def test_trim_jd_about_the_role_is_kept_not_treated_as_company_blurb():
    text = (
        "**About the Role**\n"
        "You will own the checkout flow end to end.\n\n"
        "**About Us**\n"
        "We are a company founded by three friends.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "own the checkout flow" in out
    assert "founded by three friends" not in out


def test_trim_jd_atx_headers_supported():
    text = (
        "This role involves supporting a research study.\n\n"
        "## Responsibilities\n"
        "- Conduct interviews.\n\n"
        "## Perks\n"
        "- Flexible schedule and swag.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "Conduct interviews" in out
    assert "swag" not in out


def test_trim_jd_plain_title_case_headers_supported():
    text = (
        "The Role\n"
        "You'll partner directly with the CEO.\n\n"
        "What You'll Do\n"
        "- Drive strategic initiatives end to end.\n\n"
        "Why You'll Love Working Here\n"
        "- Amazing culture and free snacks.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "Drive strategic initiatives" in out
    assert "free snacks" not in out


def test_trim_jd_eligibility_override_keeps_flagged_section_despite_boilerplate_header():
    text = (
        "**Responsibilities**\n"
        "- Build things.\n\n"
        "**Compensation and Benefits**\n"
        "This role requires an active security clearance and US citizenship. "
        "We also offer a 401k match.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "security clearance" in out
    assert "US citizenship" in out


def test_trim_jd_keeps_role_content_that_follows_boilerplate_with_no_header():
    # Observed on a real posting: role-summary paragraphs sat right after the
    # "About Company" mission blurb, separated only by a blank line, with no
    # header of their own. Dropping the whole "About Company" span would
    # have silently deleted them.
    text = (
        "**About Acme**\n"
        "Founded in 2015, Acme has raised $50M from investors.\n\n"
        "As a Senior Engineer on the Platform team, you will own the "
        "checkout service end to end.\n\n"
        "This role reports to the Director of Engineering.\n\n"
        "**Benefits**\n"
        "- 401k match\n"
        "- Free lunch\n"
    )
    out = trim_job_description_for_llm(text)
    assert "raised $50M from investors" not in out
    assert "own the checkout service end to end" in out
    assert "reports to the Director of Engineering" in out
    assert "401k match" not in out
    assert "Free lunch" not in out


def test_trim_jd_header_glued_to_body_with_no_blank_line():
    # Some postings put the header and its first content line back-to-back
    # with no blank line at all — must still be recognized as a header.
    text = (
        "The Role\n"
        "You'll partner directly with the CEO.\n\n"
        "About Us\n"
        "We were founded by three friends in a garage.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "partner directly with the CEO" in out
    assert "founded by three friends" not in out


def test_trim_jd_about_you_is_kept_as_qualifications_not_company_blurb():
    # "About You" is a common stand-in for a qualifications section on real
    # postings — must not be treated as "About Us" company boilerplate.
    text = (
        "**About Us**\n"
        "We were founded by three friends in a garage.\n\n"
        "**About you**\n"
        "- You have 5+ years of experience in client-facing roles.\n"
        "- You're confident communicating with senior stakeholders.\n"
    )
    out = trim_job_description_for_llm(text)
    assert "founded by three friends" not in out
    assert "5+ years of experience in client-facing roles" in out
    assert "confident communicating with senior stakeholders" in out


def test_trim_jd_no_headers_returns_text_unchanged():
    text = "This is a run-on job description with no line breaks or headers at all whatsoever here."
    assert trim_job_description_for_llm(text) == text


def test_trim_jd_empty_string_unchanged():
    assert trim_job_description_for_llm("") == ""


def test_trim_jd_falls_back_when_trimmed_result_collapses():
    text = "**About Us**\n" + ("We are a great company. " * 40) + "\n\n**Benefits**\n- Snacks.\n"
    out = trim_job_description_for_llm(text)
    assert out == text
