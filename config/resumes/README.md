# Resumes

Drop your resume as a PDF or DOCX directly in this folder. On the next run,
Module 2.5 (`src/coldstart/resume_ingest.py`) extracts the text, classifies
it into one of four slots by filename (falling back to an LLM call only if
the filename is ambiguous), writes a normalized `resume_<slot>.json`, and
moves the original into `originals/`.

Filename convention (case-insensitive, matched against the filename stem):
- contains `fde` → FDE flavor
- contains `ai`, `agentic`, or `ml` → AI flavor
- contains `swe`, `general`, `standard`, or `software` → confirms the
  non-FDE, non-AI (general) flavor

| Stem contains | Slot |
|---|---|
| none of the markers below | *ambiguous* — escalates to a one-time LLM content classification, logged as a warning |
| `swe`/`general`/`standard`/`software` only | A — general SWE |
| `ai`/`agentic`/`ml` only | B — AI/Agentic Engineer |
| `fde` only | C — FDE, general |
| `fde` + `ai`/`agentic`/`ml` | D — FDE, AI-specific |

A name needs at least one positive marker — a fully generic name like
`resume.pdf` is not silently assumed to be the general-SWE resume; it's
genuinely ambiguous and reading the content is the only way to be sure.

See `scope.md` §5.1 for the full design. Everything in this folder except
this file is gitignored — resumes contain PII and must never be committed.
