# Resumes

Drop your resume as a PDF or DOCX directly in this folder. On the next run,
Module 2.5 (`src/coldstart/resume_ingest.py`) extracts the text, classifies
it into one of four slots by filename (falling back to an LLM call only if
the filename is ambiguous), writes a normalized `resume_<slot>.json`, and
moves the original into `originals/`.

Filename convention (case-insensitive, matched against the filename stem):
- contains `fde` → FDE flavor
- contains `ai`, `agentic`, or `ml` → AI flavor

| Stem contains | Slot |
|---|---|
| neither | A — general SWE |
| `ai`/`agentic`/`ml` only | B — AI/Agentic Engineer |
| `fde` only | C — FDE, general |
| `fde` + `ai`/`agentic`/`ml` | D — FDE, AI-specific |

See `scope.md` §5.1 for the full design. Everything in this folder except
this file is gitignored — resumes contain PII and must never be committed.
