"""
Agent Village — Interpretation Layer
======================================
Turns a model's free-form answer into the WorkResult output schema
(`{"gaps": [...]}`), in three ordered stages, cheapest/most-deterministic
first (docs/research/AGENT_LOOP_WORKER_02.md §3):

  (a) deterministic extraction from an explicitly marked result block
      (RESULT_BEGIN/RESULT_END), if the model followed instructions;
  (b) tolerant parsing of whatever JSON-looking structure exists in the
      text, for when the model produced the right content without the
      exact markers;
  (c) LAST RESORT ONLY, and never automatically: a second LLM call whose
      SOLE job is reformatting an already-produced answer into the exact
      schema. This module builds that constrained prompt
      (`build_interpretation_prompt()`); village/worker.py decides
      whether/when to spend a call on it (bounded by
      MAX_LLM_CALLS_PER_EXECUTION).

None of these stages perform new analysis. (a) and (b) are pure text
processing -- no LLM call, no judgment of content quality, only shape.
(c)'s prompt explicitly forbids the model from adding anything not
already present in the prior answer -- enforced in the prompt text
itself, not just asserted in a comment (docs/research/
AGENT_LOOP_WORKER_02.md's explicit requirement).
"""

from __future__ import annotations

import json
import re

RESULT_BEGIN = "===RESULT_BEGIN==="
RESULT_END = "===RESULT_END==="


def _validate_structure(parsed: object) -> tuple[dict | None, str | None]:
    """Deterministic STRUCTURAL validation only -- never a judgment of
    whether the analysis is any good, only whether it's the shape asked
    for. Shared by all three interpretation stages so the acceptance
    criteria are identical regardless of how the JSON was found."""
    if not isinstance(parsed, dict) or "gaps" not in parsed:
        return None, "output JSON missing required 'gaps' key"
    if not isinstance(parsed["gaps"], list):
        return None, "'gaps' must be a list"
    for i, gap in enumerate(parsed["gaps"]):
        if not isinstance(gap, dict) or "description" not in gap or "file" not in gap:
            return None, f"gaps[{i}] missing required 'description'/'file' fields"
    return parsed, None


def _strip_fence(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()


def extract_marked_block(text: str) -> tuple[dict | None, str | None]:
    """Stage (a): deterministic extraction between explicit markers.
    Returns (None, reason) if the markers are missing or don't contain
    valid+well-shaped JSON."""
    start = text.find(RESULT_BEGIN)
    end = text.find(RESULT_END)
    if start == -1 or end == -1 or end <= start:
        return None, "no marked result block found"
    block = text[start + len(RESULT_BEGIN):end]
    block = _strip_fence(block)
    try:
        parsed = json.loads(block)
    except ValueError as exc:
        return None, f"marked result block is not valid JSON: {exc}"
    return _validate_structure(parsed)


def tolerant_parse(text: str) -> tuple[dict | None, str | None]:
    """Stage (b): no markers required. Tries the whole text as JSON
    first (after stripping an accidental markdown fence), then falls
    back to the first balanced `{...}` span found anywhere in the text
    -- still purely deterministic text processing, no LLM call."""
    stripped = _strip_fence(text)
    try:
        return _validate_structure(json.loads(stripped))
    except ValueError:
        pass

    # First balanced-brace JSON object anywhere in the text.
    depth = 0
    start_idx = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx is not None:
                    candidate = text[start_idx:i + 1]
                    try:
                        parsed = json.loads(candidate)
                    except ValueError:
                        continue
                    result, err = _validate_structure(parsed)
                    if result is not None:
                        return result, None
    return None, "no usable JSON object found in free text"


def build_interpretation_prompt(prior_text: str) -> str:
    """Stage (c)'s prompt. The no-new-analysis constraint is enforced
    IN THE PROMPT TEXT, not just documented in a comment -- repeated
    twice, deliberately, since this is the one LLM call in the whole
    loop that must not be allowed to invent anything."""
    return (
        "You are a strict reformatting tool, not an analyst. Below is a "
        "prior answer that already contains an analysis. Your ONLY job "
        "is to extract what is already stated there into this exact "
        "JSON shape:\n"
        '{"gaps": [{"description": "string", "file": "string", "line": integer-or-null}]}\n\n'
        "Rules, all mandatory:\n"
        "- Do NOT perform any new analysis.\n"
        "- Do NOT add any gap, fact, or file reference that is not "
        "already present, in substance, in the prior answer below.\n"
        "- If the prior answer contains no identifiable gaps, return "
        '{"gaps": []}.\n'
        "- Reply with ONLY the JSON object, nothing else, no markdown "
        "fences, no commentary.\n\n"
        f"--- PRIOR ANSWER ---\n{prior_text}\n--- END PRIOR ANSWER ---"
    )
