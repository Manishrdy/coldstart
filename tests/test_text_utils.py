from coldstart.text_utils import strip_markdown_json_fences


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
