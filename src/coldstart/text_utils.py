from __future__ import annotations


def strip_markdown_json_fences(text: str) -> str:
    """Strip a ```json ... ``` or ``` ... ``` wrapper some LLMs add around
    JSON output despite being told not to."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return cleaned
