"""
Tests for village/interpreter.py -- the three-stage interpretation
layer (marked-block extraction, tolerant parsing, interpretation-prompt
construction). No LLM calls here -- stage (c) is only prompt
construction; actually spending a call on it is village/worker.py's job.
"""

from __future__ import annotations

from village.interpreter import (
    RESULT_BEGIN,
    RESULT_END,
    build_interpretation_prompt,
    extract_marked_block,
    tolerant_parse,
)


# ── Stage (a): marked block extraction ────────────────────────────────────


def test_extracts_valid_marked_block():
    text = f'some reasoning here\n{RESULT_BEGIN}\n{{"gaps": [{{"description": "x", "file": "y.py"}}]}}\n{RESULT_END}\nmore notes'
    parsed, err = extract_marked_block(text)
    assert err is None
    assert parsed == {"gaps": [{"description": "x", "file": "y.py"}]}


def test_marked_block_tolerates_markdown_fence_inside():
    text = f'{RESULT_BEGIN}\n```json\n{{"gaps": []}}\n```\n{RESULT_END}'
    parsed, err = extract_marked_block(text)
    assert err is None
    assert parsed == {"gaps": []}


def test_no_markers_returns_none_with_reason():
    parsed, err = extract_marked_block("just some free text, no markers at all")
    assert parsed is None
    assert "no marked result block" in err


def test_markers_present_but_invalid_json_returns_none_with_reason():
    text = f"{RESULT_BEGIN}\nnot json\n{RESULT_END}"
    parsed, err = extract_marked_block(text)
    assert parsed is None
    assert "not valid JSON" in err


def test_markers_present_but_wrong_shape_returns_none_with_reason():
    text = f'{RESULT_BEGIN}\n{{"wrong_key": true}}\n{RESULT_END}'
    parsed, err = extract_marked_block(text)
    assert parsed is None
    assert "gaps" in err


# ── Stage (b): tolerant parsing ────────────────────────────────────────────


def test_tolerant_parse_whole_text_as_json():
    parsed, err = tolerant_parse('{"gaps": [{"description": "x", "file": "y.py"}]}')
    assert err is None
    assert parsed["gaps"][0]["file"] == "y.py"


def test_tolerant_parse_finds_json_object_in_free_text():
    text = 'Here is my analysis: {"gaps": [{"description": "missing tests", "file": "a.py"}]} Hope this helps!'
    parsed, err = tolerant_parse(text)
    assert err is None
    assert parsed["gaps"][0]["description"] == "missing tests"


def test_tolerant_parse_finds_correct_object_among_multiple_braces():
    text = 'Some unrelated {nonsense} text. {"gaps": []} trailing.'
    parsed, err = tolerant_parse(text)
    assert err is None
    assert parsed == {"gaps": []}


def test_tolerant_parse_returns_none_when_nothing_usable():
    parsed, err = tolerant_parse("no json anywhere in this text")
    assert parsed is None
    assert "no usable JSON" in err


# ── Stage (c): interpretation prompt construction ──────────────────────────


def test_interpretation_prompt_forbids_new_analysis_explicitly():
    prompt = build_interpretation_prompt("prior answer text here")
    assert "Do NOT perform any new analysis" in prompt
    assert "Do NOT add any gap" in prompt


def test_interpretation_prompt_includes_the_prior_answer_verbatim():
    prompt = build_interpretation_prompt("UNIQUE_MARKER_TEXT_12345")
    assert "UNIQUE_MARKER_TEXT_12345" in prompt


def test_interpretation_prompt_requires_json_only_reply():
    prompt = build_interpretation_prompt("x")
    assert "ONLY the JSON object" in prompt
