"""
MOLTBOOK CAPTCHA — Deterministic Verify-Challenge Solver
==========================================================

Ported from kimeisele/steward-protocol, as of commit
34a8a0efc25c15ef7c07dd4fb50aeb2510c071e8 (2026-07-12):
  - ChallengeSolver:  vibe_core/mahamantra/adapters/moltbook.py, lines 161-409
  - CaptchaChamber:   vibe_core/mahamantra/adapters/captcha_decoder.py (complete)

Algorithm unchanged except for one documented substitution (see below).
Read-only reference only — steward-protocol itself was never modified.

DIVERGES FROM SOURCE (transport layer):
The source's comment_with_verification() expected the Moltbook API to return
`error: "VERIFICATION_REQUIRED"` directly in the first POST /comments response,
with challenge_id/challenge_solution sent back in a SECOND POST to the SAME
/comments endpoint. Live testing against the current API (see
agent-village/docs/BEFUND.md §3) shows a different contract: the comment is
created immediately (201) with `verification_status: "pending"` and a
`verification` object containing `verification_code` + `challenge_text`; the
solved answer must go to a SEPARATE `POST /api/v1/verify` call. This module
only ports the solving algorithm (protocol-agnostic — it just takes challenge
text and returns an answer string). The two-step HTTP flow adapted to the
current contract lives in `solve_and_verify()` at the bottom of this file, and
is new code, not ported.

DIVERGES FROM SOURCE (dependency): the source's most aggressive decoding
strategy (_pada_aggressive) used a Sanskrit-phonetic fuzzy matcher (the "RAMA"
system: encode_text/basin_cosine/hkr_similarity) as its single-token fuzzy
fallback. Pulling that in would mean importing ~1800 unrelated lines
(phonetic_encoder.py, basin_map.py, rama_grid.py, pancha_walk.py,
varnamala_codec.py, protocols/_seed.py) — a self-contained Sanskrit-linguistics
subsystem with no other purpose here. Replaced with stdlib
difflib.SequenceMatcher: same role (fuzzy single-token vocabulary match),
different metric. Confidence-threshold recalibration check against all known
real challenge samples: see agent-village/docs/BEFUND.md §5.

Everything else — the 4-strategy generate/score/decide architecture, the 6
scorers, the confidence threshold, the "None means skip, never guess"
principle, and the ChallengeMonitor ban-avoidance halt — is unchanged.
"""

from __future__ import annotations

import ast
import difflib
import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, Final, List, Optional, Sequence, Tuple, cast

logger = logging.getLogger("MOLTBOOK_CAPTCHA")


# =============================================================================
# CHALLENGE MONITOR (ban avoidance) — ported unchanged from moltbook.py
# =============================================================================


class ChallengeMonitor:
    """Tracks challenge solve attempts for ban avoidance.

    10 consecutive failures = ban. This monitor detects failure trends
    and halts commenting before the ban threshold.
    """

    BAN_THRESHOLD = 10  # Max allowed failures before ban
    HALT_THRESHOLD = 5  # Stop attempting after this many consecutive failures

    def __init__(self):
        self._consecutive_failures: int = 0
        self._total_attempts: int = 0
        self._total_successes: int = 0
        self._total_failures: int = 0
        self._halted: bool = False
        self._last_challenge_format: str = ""  # Track format changes

    @property
    def is_halted(self) -> bool:
        """True when too many consecutive failures — stop commenting."""
        return self._halted

    @property
    def failure_rate(self) -> float:
        if self._total_attempts == 0:
            return 0.0
        return self._total_failures / self._total_attempts

    def record_success(self) -> None:
        """Record a successful challenge solve."""
        self._consecutive_failures = 0
        self._total_attempts += 1
        self._total_successes += 1
        if self._halted:
            self._halted = False
            logger.info("Challenge monitor: resumed after successful solve")

    def record_failure(self, challenge_text: str = "") -> None:
        """Record a failed challenge solve."""
        self._consecutive_failures += 1
        self._total_attempts += 1
        self._total_failures += 1

        if self._consecutive_failures >= self.HALT_THRESHOLD:
            self._halted = True
            logger.error(
                f"CHALLENGE MONITOR HALT: {self._consecutive_failures} consecutive failures. "
                f"Commenting suspended to avoid ban. "
                f"Last challenge: {challenge_text[:100]}"
            )

    def check_format_change(self, challenge_text: str) -> bool:
        """Detect if the challenge format has changed.

        Returns True if format appears different from previous challenges.
        """
        text = challenge_text.lower().strip()
        has_digits = bool(re.search(r"\d", text))
        has_operator = any(op in text for op in ["+", "-", "*", "/", "plus", "minus", "times", "divided"])
        has_question = text.startswith("what")

        fmt = f"digits={has_digits}|op={has_operator}|q={has_question}"

        if self._last_challenge_format and fmt != self._last_challenge_format:
            logger.warning(
                f"Challenge format change detected! "
                f"Was: {self._last_challenge_format}, Now: {fmt}. "
                f"Challenge: {challenge_text[:100]}"
            )
            self._last_challenge_format = fmt
            return True

        self._last_challenge_format = fmt
        return False

    def get_stats(self) -> dict:
        """Return monitoring stats for diagnostics."""
        return {
            "total_attempts": self._total_attempts,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "consecutive_failures": self._consecutive_failures,
            "failure_rate": self.failure_rate,
            "halted": self._halted,
        }

    def to_state(self) -> dict:
        """Serialize counters for cross-process/cross-cycle persistence.

        New code, not ported — the source ChallengeMonitor was designed for
        a long-running daemon process where in-memory state is enough. Each
        village heartbeat run is a fresh process, so BAN_THRESHOLD (10)
        can only be meaningfully enforced across cycles if something
        outside this class persists and restores these counters. See
        village/heartbeat.py's challenge_failures.json handling and
        docs/BEFUND.md §8/§9.
        """
        return {
            "consecutive_failures": self._consecutive_failures,
            "total_attempts": self._total_attempts,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "halted": self._halted,
        }

    def load_state(self, state: dict) -> None:
        """Restore counters from a persisted dict. See to_state()."""
        self._consecutive_failures = state.get("consecutive_failures", 0)
        self._total_attempts = state.get("total_attempts", 0)
        self._total_successes = state.get("total_successes", 0)
        self._total_failures = state.get("total_failures", 0)
        self._halted = state.get("halted", False)


# Module-level singleton — shared across all client instances, matching source.
_challenge_monitor = ChallengeMonitor()


def get_challenge_monitor() -> ChallengeMonitor:
    """Get the global challenge monitor singleton."""
    return _challenge_monitor


# =============================================================================
# CHALLENGE SOLVER — ported unchanged from moltbook.py
# =============================================================================


class ChallengeSolver:
    """
    Solves Moltbook's obfuscated math challenges.
    Failure = temporary ban. This MUST be flawless.

    Two-layer architecture:
    1. SAFE EXPRESSION EVALUATOR (primary) — handles decimals, chained ops,
       operator precedence, parentheses via Python AST. No eval(). No exec().
    2. REGEX FALLBACK (secondary) — legacy 4-operator solver for edge cases.
    """

    # Compound numbers MUST come before their substrings to prevent
    # "eighteen" → "8een" corruption. Order matters.
    WORD_MAP = [
        # Compound teens/tens FIRST (prevent substring corruption)
        ("eighteen", "18"),
        ("seventeen", "17"),
        ("sixteen", "16"),
        ("fifteen", "15"),
        ("fourteen", "14"),
        ("thirteen", "13"),
        ("twelve", "12"),
        ("eleven", "11"),
        ("nineteen", "19"),
        ("eighty", "80"),
        ("seventy", "70"),
        ("sixty", "60"),
        ("fifty", "50"),
        ("forty", "40"),
        ("thirty", "30"),
        ("twenty", "20"),
        ("ninety", "90"),
        ("hundred", "100"),
        ("thousand", "1000"),
        ("million", "1000000"),
        # Single digits LAST
        ("zero", "0"),
        ("one", "1"),
        ("two", "2"),
        ("three", "3"),
        ("four", "4"),
        ("five", "5"),
        ("six", "6"),
        ("seven", "7"),
        ("eight", "8"),
        ("nine", "9"),
        ("ten", "10"),
    ]

    # Operator word → symbol substitution (applied AFTER number substitution)
    OPERATOR_MAP = [
        ("plus", "+"),
        ("add", "+"),
        ("sum of", "+"),
        ("minus", "-"),
        ("subtract", "-"),
        ("difference", "-"),
        ("times", "*"),
        ("multiply", "*"),
        ("multiplied by", "*"),
        ("divided by", "/"),
        ("divide", "/"),
        ("modulo", "%"),
        ("mod", "%"),
        ("remainder", "%"),
        ("power of", "**"),
        ("raised to", "**"),
        ("squared", "**2"),
        ("cubed", "**3"),
    ]

    # Allowed AST node types for safe evaluation (NO exec/import/call)
    _SAFE_NODES = {
        ast.Expression,
        ast.BinOp,
        ast.UnaryOp,
        ast.Constant,  # Python 3.8+: numbers, strings
        ast.Num,  # Python 3.7 compat
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
        ast.USub,  # Unary minus (-x)
        ast.UAdd,  # Unary plus (+x)
    }

    @staticmethod
    def _safe_eval(expr: str) -> Optional[float]:
        """Evaluate a math expression safely using AST whitelist.

        Only allows arithmetic operations on numbers. No function calls,
        no variable access, no imports, no exec. Returns None on any error.
        """
        try:
            tree = ast.parse(expr.strip(), mode="eval")
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if type(node) not in ChallengeSolver._SAFE_NODES:
                logger.debug(f"Unsafe AST node rejected: {type(node).__name__}")
                return None

        try:
            result = eval(compile(tree, "<challenge>", "eval"))  # noqa: S307
            if isinstance(result, (int, float)):
                return result
        except (ZeroDivisionError, OverflowError, ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _normalize_text(challenge_text: str) -> str:
        """Convert challenge text to evaluable math expression.

        Order matters:
        1. Resolve hyphenated compounds (twenty-three → twentythree)
        2. Replace word-numbers with digits
        3. Join adjacent tens+units (20 3 → 23)
        4. Replace operator words with symbols
        5. Clean up non-math characters
        """
        text = challenge_text.lower()

        _HYPHEN_COMPOUNDS = re.compile(
            r"\b(twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
            r"[-\s]+"
            r"(one|two|three|four|five|six|seven|eight|nine)\b"
        )
        _UNITS = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
        }
        _TENS = {
            "twenty": 20,
            "thirty": 30,
            "forty": 40,
            "fifty": 50,
            "sixty": 60,
            "seventy": 70,
            "eighty": 80,
            "ninety": 90,
        }

        def _resolve_compound(m: re.Match) -> str:
            return str(_TENS[m.group(1)] + _UNITS[m.group(2)])

        text = _HYPHEN_COMPOUNDS.sub(_resolve_compound, text)

        for word, num in ChallengeSolver.WORD_MAP:
            text = re.sub(rf"\b{word}\b", num, text)

        text = re.sub(
            r"\b([2-9]0)\s+([1-9])\b",
            lambda m: str(int(m.group(1)) + int(m.group(2))),
            text,
        )

        for word, symbol in ChallengeSolver.OPERATOR_MAP:
            text = re.sub(rf"\b{re.escape(word)}\b", f" {symbol} ", text)

        text = re.sub(r"\*\*\s*\*\*", "**", text)

        text = re.sub(r"[^0-9.+\-*/%() ]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text

    @staticmethod
    def solve(challenge_text: str) -> str:
        """
        Solve a math challenge from obfuscated text.

        Two-layer approach:
        1. Normalize → safe AST eval (handles decimals, chained ops, precedence)
        2. Fallback: regex extraction + single operator (legacy)

        Always returns a string. Never raises.
        """
        expr = ChallengeSolver._normalize_text(challenge_text)
        if expr:
            result = ChallengeSolver._safe_eval(expr)
            if result is not None:
                if result == int(result):
                    return str(int(result))
                return str(result)

        return ChallengeSolver._solve_regex_fallback(challenge_text)

    @staticmethod
    def _solve_regex_fallback(challenge_text: str) -> str:
        """Legacy regex solver — handles simple single-operator challenges.

        Kept as fallback for edge cases where AST normalization fails.
        """
        text = challenge_text.lower()

        for word, num in ChallengeSolver.WORD_MAP:
            text = re.sub(rf"\b{word}\b", num, text)

        numbers = [int(n) for n in re.findall(r"\d+", text)]

        if len(numbers) < 2:
            logger.warning(f"Could not parse math challenge: '{challenge_text}'")
            return "0"

        if "+" in text or "plus" in text or "add" in text:
            return str(sum(numbers))
        elif "-" in text or "minus" in text or "subtract" in text:
            return str(numbers[0] - sum(numbers[1:]))
        elif "*" in text or "times" in text or "multiply" in text:
            result = 1
            for n in numbers:
                result *= n
            return str(result)
        elif "/" in text or "divided" in text:
            if numbers[1] != 0:
                return str(numbers[0] // numbers[1])
            return "0"

        logger.warning(f"Unknown operator in challenge: '{challenge_text}'")
        return "0"


# =============================================================================
# CAPTCHA CHAMBER — ported from captcha_decoder.py, RAMA fuzzy step replaced
# =============================================================================

CONFIDENCE_THRESHOLD: Final[float] = 2.25  # Out of max 6.0 (37.5%) — unchanged from source


@dataclass
class CaptchaCandidate:
    """One candidate answer from one strategy."""

    answer: str
    expression: str
    decoded_text: str
    strategy: str
    scores: Dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0


_NUMBER_WORDS: Final[Dict[str, int]] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
    "thousand": 1000,
}

_OPERATOR_WORDS: Final[Dict[str, str]] = {
    "plus": "+",
    "add": "+",
    "sum": "+",
    "minus": "-",
    "subtract": "-",
    "difference": "-",
    "times": "*",
    "multiply": "*",
    "divided": "/",
    "divide": "/",
    "modulo": "%",
    "mod": "%",
    "remainder": "%",
    # Added after BEFUND.md §5 / live E2E test: these must be in the
    # reconstruction vocabulary, not just _extract_math's trigger-word
    # list — without a vocab entry, _pada_collapse/_pada_aggressive have no
    # reason to reassemble them and they stay as unrecognized fragments
    # (e.g. "acceeleratesby"), so the trigger-word substring check on the
    # decoded text never finds them either.
    "gains": "+",
    "gain": "+",
    "accelerates": "+",
    "loses": "-",
    "lose": "-",
    "looses": "-",
    "decelerates": "-",
}

_CONTEXT_WORDS: Final[Tuple[str, ...]] = ("total", "combined", "altogether", "together", "and")

# DIVERGES FROM SOURCE: source built _VOCAB_COORDS (word -> RAMA coordinate
# tuple via encode_text) for both exact/collapsed dict-membership lookups AND
# the aggressive strategy's phonetic fuzzy match. We only need plain
# vocabulary membership (dict/set), never the coordinate values themselves,
# except in the fuzzy step — which now uses difflib instead. So this is a
# plain word set, no phonetic encoding, no lazy RAMA init.
_VOCAB_WORDS: set = set()
_VOCAB_COLLAPSED: Dict[str, str] = {}
_vocab_initialized: bool = False


def _ensure_vocab() -> None:
    """Pre-index math vocabulary for matching. Lazy init."""
    global _vocab_initialized
    if _vocab_initialized:
        return
    all_words = list(_NUMBER_WORDS.keys()) + list(_OPERATOR_WORDS.keys()) + list(_CONTEXT_WORDS)
    for word in all_words:
        _VOCAB_WORDS.add(word)
        collapsed = _collapse_all(word)
        _VOCAB_COLLAPSED[collapsed] = word
        _VOCAB_COLLAPSED[word] = word
    _vocab_initialized = True


def _varna_filter(text: str) -> str:
    """Noise strip. Alpha → lowercase, digits survive, rest → space."""
    result: list = []
    for ch in text:
        if ch.isalpha():
            result.append(ch.lower())
        elif ch.isdigit():
            result.append(ch)
        else:
            result.append(" ")
    return " ".join("".join(result).split())


def _akshara_collapse(text: str) -> str:
    """Collapse runs of 3+ identical chars → single. Preserves doubles (ee in three)."""
    if len(text) < 3:
        return text
    result: list = []
    i = 0
    while i < len(text):
        ch = text[i]
        j = i + 1
        while j < len(text) and text[j] == ch:
            j += 1
        run_len = j - i
        if run_len >= 3:
            result.append(ch)
        else:
            result.extend(ch for _ in range(run_len))
        i = j
    return "".join(result)


def _collapse_all(s: str) -> str:
    """Collapse ALL consecutive identical chars → single. 'foorty' → 'forty'."""
    if not s:
        return s
    result = [s[0]]
    for ch in s[1:]:
        if ch != result[-1]:
            result.append(ch)
    return "".join(result)


def _pada_exact(tokens: List[str], max_window: int = 6) -> List[str]:
    """Reassemble fragments using exact vocabulary match only."""
    _ensure_vocab()
    result: list = []
    i = 0
    while i < len(tokens):
        best_word: Optional[str] = None
        best_len = 0
        for window in range(min(max_window, len(tokens) - i), 1, -1):
            candidate = "".join(tokens[i : i + window])
            if len(candidate) > 12:
                continue
            if candidate in _VOCAB_WORDS:
                best_word = candidate
                best_len = window
                break
        if best_word and best_len > 1:
            result.append(best_word)
            i += best_len
        else:
            result.append(tokens[i])
            i += 1
    return result


def _pada_collapse(tokens: List[str], max_window: int = 8) -> List[str]:
    """DP-based reassembly: maximize recognized token coverage, minimize segments."""
    _ensure_vocab()
    n = len(tokens)
    if n == 0:
        return []

    _WORST: Tuple[int, int] = (-1, -n - 1)
    dp: list = [(_WORST, [])] * (n + 1)
    dp[n] = ((0, 0), [])

    for i in range(n - 1, -1, -1):
        best_score = _WORST
        best_words: list = []
        mw = min(max_window, n - i)

        for w in range(1, mw + 1):
            future_score, future_words = dp[i + w]
            if future_score == _WORST:
                continue

            candidate = "".join(tokens[i : i + w])
            if len(candidate) > 14:
                continue

            word: Optional[str] = None
            is_rec = False

            if candidate in _VOCAB_WORDS:
                word = candidate
                is_rec = True
            else:
                collapsed = _collapse_all(candidate)
                if collapsed in _VOCAB_COLLAPSED:
                    word = _VOCAB_COLLAPSED[collapsed]
                    is_rec = True

            if word is None:
                word = candidate

            covered = (w if is_rec else 0) + future_score[0]
            segs = -1 + future_score[1]
            score = (covered, segs)

            if score > best_score:
                best_score = score
                best_words = [word] + future_words

        dp[i] = (best_score, best_words)

    result = dp[0][1]

    final: list = []
    for w in result:
        if w in _VOCAB_WORDS:
            final.append(w)
        else:
            collapsed = _collapse_all(w)
            if collapsed in _VOCAB_COLLAPSED:
                final.append(_VOCAB_COLLAPSED[collapsed])
            else:
                final.append(w)
    return final


def _pada_aggressive(tokens: List[str], max_window: int = 10) -> List[str]:
    """DP-based aggressive reassembly: collapse + fuzzy match for unmatched tokens.

    DIVERGES FROM SOURCE: the single-token fuzzy fallback used the RAMA
    phonetic similarity metric (encode_text + basin_cosine/hkr_similarity)
    in steward-protocol. Replaced with difflib.SequenceMatcher — same role
    (fuzzy-match one long unrecognized token against the vocabulary), a
    different metric. Acceptance floor (0.95) kept identical to source;
    length-difference prefilter adapted from RAMA-coordinate-length (>1) to
    plain character-length (>2), since difflib compares raw strings, not
    phoneme counts. See agent-village/docs/BEFUND.md §5 for the calibration
    check against real samples.
    """
    _ensure_vocab()
    n = len(tokens)
    if n == 0:
        return []

    _WORST: Tuple[int, int] = (-1, -n - 1)
    dp: list = [(_WORST, [])] * (n + 1)
    dp[n] = ((0, 0), [])

    for i in range(n - 1, -1, -1):
        best_score = _WORST
        best_words: list = []
        mw = min(max_window, n - i)

        for w in range(1, mw + 1):
            future_score, future_words = dp[i + w]
            if future_score == _WORST:
                continue

            candidate = "".join(tokens[i : i + w])
            if len(candidate) > 16:
                continue

            word: Optional[str] = None
            is_rec = False

            if candidate in _VOCAB_WORDS:
                word = candidate
                is_rec = True
            else:
                collapsed = _collapse_all(candidate)
                if collapsed in _VOCAB_COLLAPSED:
                    word = _VOCAB_COLLAPSED[collapsed]
                    is_rec = True

            # Fuzzy fallback (difflib, see docstring above) — single long
            # tokens with no exact/collapsed match.
            if word is None and w == 1 and len(tokens[i]) >= 6:
                best_sim = 0.95
                for vword in _VOCAB_WORDS:
                    if abs(len(tokens[i]) - len(vword)) > 2:
                        continue
                    sim = difflib.SequenceMatcher(None, tokens[i], vword).ratio()
                    if sim > best_sim:
                        best_sim = sim
                        word = vword
                        is_rec = True

            if word is None:
                word = candidate

            covered = (w if is_rec else 0) + future_score[0]
            segs = -1 + future_score[1]
            score = (covered, segs)

            if score > best_score:
                best_score = score
                best_words = [word] + future_words

        dp[i] = (best_score, best_words)

    result = dp[0][1]

    final: list = []
    for w in result:
        if w in _VOCAB_WORDS:
            final.append(w)
        else:
            collapsed = _collapse_all(w)
            if collapsed in _VOCAB_COLLAPSED:
                final.append(_VOCAB_COLLAPSED[collapsed])
            else:
                final.append(w)
    return final


_TENS_WORDS: Final[frozenset] = frozenset(
    {"twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"}
)
_ONES_WORDS: Final[frozenset] = frozenset({"one", "two", "three", "four", "five", "six", "seven", "eight", "nine"})


def _merge_compounds(words: List[str]) -> List[str]:
    """Merge adjacent tens + ones into hyphenated compounds.

    After varna_filter strips hyphens, "eighty-four" → ["eighty", "four"].
    This step restores them: ["eighty", "four"] → ["eighty-four"].
    ChallengeSolver._normalize_text then handles "eighty-four" → "84".
    """
    result: list = []
    i = 0
    while i < len(words):
        if i + 1 < len(words) and words[i] in _TENS_WORDS and words[i + 1] in _ONES_WORDS:
            result.append(f"{words[i]}-{words[i + 1]}")
            i += 2
        else:
            result.append(words[i])
            i += 1
    return result


def _extract_math(decoded_text: str) -> Optional[str]:
    """Extract math expression from decoded text."""
    expr = ChallengeSolver._normalize_text(decoded_text)
    if expr:
        result = ChallengeSolver._safe_eval(expr)
        if result is not None:
            return expr

    search_text = expr if expr else decoded_text
    numbers = re.findall(r"\d+\.?\d*", search_text)
    if not numbers:
        return None
    if len(numbers) == 1:
        return cast(str, numbers[0])

    text_lower = decoded_text.lower()

    # "loses"/"looses" (subtraction) and "gains"/"accelerates"/"and" (addition)
    # added after BEFUND.md §5: samples 3/5 used "and .. accelerates by" /
    # "and gains" with no explicit operator word; a live test used "but it
    # looses" for subtraction. "and" is listed last and only reached if no
    # more specific word matched — kept broad per Kim's instruction, but
    # lowest priority since it's the least specific signal.
    _EXP_MINUS = ("minus", "subtract", "loses", "lose", "looses", "decelerates")
    _EXP_TIMES = ("times", "multiply")
    _EXP_DIV = ("divided", "divide")
    _EXP_PLUS = ("plus", "add", "gains", "gain", "accelerates")
    if any(w in text_lower for w in _EXP_MINUS):
        return " - ".join(numbers)
    if any(w in text_lower for w in _EXP_TIMES):
        return " * ".join(numbers)
    if any(w in text_lower for w in _EXP_DIV):
        return " / ".join(numbers)
    if any(w in text_lower for w in _EXP_PLUS):
        return " + ".join(numbers)

    _SUM = ("total", "sum", "altogether", "combined", "together", "all", "both")
    _DIFF = ("difference", "less", "fewer", "remaining", "left")
    _PROD = ("product",)
    if any(w in text_lower for w in _SUM):
        return " + ".join(numbers)
    if any(w in text_lower for w in _DIFF):
        return " - ".join(numbers)
    if any(w in text_lower for w in _PROD):
        return " * ".join(numbers)

    # Lowest-priority fallback: bare "and" with exactly two numbers and no
    # other recognized signal. Least specific of all triggers (many
    # sentences use "and" without meaning addition) — only reached here
    # because everything more specific above already failed.
    if "and" in text_lower.split():
        return " + ".join(numbers)

    return None


def _safe_eval_expr(expr: str) -> Optional[str]:
    """Evaluate expression → answer string. None if invalid."""
    result = ChallengeSolver._safe_eval(expr)
    if result is None:
        return None
    if isinstance(result, float) and result == int(result):
        return str(int(result))
    return str(result)


def _strategy_exact(challenge: str) -> List[CaptchaCandidate]:
    """Conservative: exact vocab match only. Windows: 4, 6, 8."""
    clean = _varna_filter(challenge)
    clean = _akshara_collapse(clean)
    tokens = clean.split()
    results: list = []
    seen_answers: set = set()
    for w in (4, 6, 8):
        words = _merge_compounds(_pada_exact(tokens, max_window=w))
        decoded = " ".join(words)
        expr = _extract_math(decoded)
        if not expr:
            continue
        answer = _safe_eval_expr(expr)
        if answer is None or answer in seen_answers:
            continue
        seen_answers.add(answer)
        results.append(CaptchaCandidate(answer=answer, expression=expr, decoded_text=decoded, strategy=f"exact_w{w}"))
    return results


def _strategy_collapse(challenge: str) -> List[CaptchaCandidate]:
    """Moderate: exact-first + collapse-all matching. Windows: 6, 8, 10."""
    clean = _varna_filter(challenge)
    clean = _akshara_collapse(clean)
    tokens = clean.split()
    results: list = []
    seen_answers: set = set()
    for w in (6, 8, 10):
        words = _merge_compounds(_pada_collapse(tokens, max_window=w))
        decoded = " ".join(words)
        expr = _extract_math(decoded)
        if not expr:
            continue
        answer = _safe_eval_expr(expr)
        if answer is None or answer in seen_answers:
            continue
        seen_answers.add(answer)
        results.append(
            CaptchaCandidate(answer=answer, expression=expr, decoded_text=decoded, strategy=f"collapse_w{w}")
        )
    return results


def _strategy_aggressive(challenge: str) -> List[CaptchaCandidate]:
    """Aggressive: wide window, collapse, difflib fuzzy. Windows: 8, 10, 12."""
    clean = _varna_filter(challenge)
    clean = _akshara_collapse(clean)
    tokens = clean.split()
    results: list = []
    seen_answers: set = set()
    for w in (8, 10, 12):
        words = _merge_compounds(_pada_aggressive(tokens, max_window=w))
        decoded = " ".join(words)
        expr = _extract_math(decoded)
        if not expr:
            continue
        answer = _safe_eval_expr(expr)
        if answer is None or answer in seen_answers:
            continue
        seen_answers.add(answer)
        results.append(
            CaptchaCandidate(answer=answer, expression=expr, decoded_text=decoded, strategy=f"aggressive_w{w}")
        )
    return results


def _strategy_direct(challenge: str) -> List[CaptchaCandidate]:
    """Direct: ChallengeSolver on raw text. For clean captchas."""
    result = ChallengeSolver.solve(challenge)
    if result == "0":
        return []
    return [CaptchaCandidate(answer=result, expression="(direct)", decoded_text=challenge, strategy="direct")]


_STRATEGIES = (_strategy_exact, _strategy_collapse, _strategy_aggressive, _strategy_direct)


def _score_expression(candidate: CaptchaCandidate, _challenge: str) -> float:
    """Valid math expression with 2+ numbers and operator → high score."""
    expr = candidate.expression
    if expr == "(direct)":
        return 0.6
    numbers = re.findall(r"\d+\.?\d*", expr)
    has_operator = bool(re.search(r"[+\-*/%]", expr))
    if len(numbers) >= 2 and has_operator:
        return 1.0
    if len(numbers) == 1:
        return 0.3
    return 0.0


def _score_consensus(
    candidate: CaptchaCandidate, _challenge: str, all_candidates: Sequence[CaptchaCandidate] = ()
) -> float:
    """How many strategies agree on this answer?"""
    if len(all_candidates) < 2:
        return 0.25
    agree = sum(1 for c in all_candidates if c.answer == candidate.answer)
    return agree / len(all_candidates)


def _score_range(candidate: CaptchaCandidate, _challenge: str) -> float:
    """Is the answer in a reasonable range for a captcha?"""
    try:
        val = float(candidate.answer)
    except (ValueError, TypeError):
        return 0.0
    if val < 0 or val > 100000:
        return 0.0
    if val != int(val):
        return 0.5
    if 0 <= val <= 10000:
        return 1.0
    return 0.5


def _is_number_word(word: str) -> bool:
    """Check if word is a recognized number (simple or compound)."""
    if word in _NUMBER_WORDS or word.isdigit():
        return True
    if "-" in word:
        parts = word.split("-", 1)
        if len(parts) == 2 and parts[0] in _NUMBER_WORDS and parts[1] in _NUMBER_WORDS:
            return True
    return False


def _score_completeness(candidate: CaptchaCandidate, _challenge: str) -> float:
    """Did the decoder find a sensible math structure?

    FIX (BEFUND.md §5/live E2E test): the original scoring gave 0.5 for
    ANY found_numbers >= 1, including a single lone number with no
    operator — an incomplete decode, not "a sensible math structure". That
    let single-number candidates clear the 2.25/6.0 confidence threshold
    and get submitted as confidently wrong answers instead of returning
    None. found_numbers == 1 now scores 0.0: a real one-operand captcha
    ("what is the number 5") is rare and, if it exists, other scorers
    (range, decode_fidelity) plus multi-strategy consensus can still carry
    it past the threshold — but an incomplete two-operand decode must not
    be rewarded as if it were complete.
    """
    text = candidate.decoded_text.lower()
    found_numbers = 0
    found_operator = False
    for word in text.split():
        if _is_number_word(word):
            found_numbers += 1
        if word in _OPERATOR_WORDS or word in _CONTEXT_WORDS:
            found_operator = True

    expr_numbers = re.findall(r"\d+\.?\d*", candidate.expression)
    if len(expr_numbers) >= 4:
        return 0.3

    if found_numbers >= 2 and found_operator:
        return 1.0
    if found_numbers >= 2:
        return 0.3
    return 0.0


def _score_decode_fidelity(candidate: CaptchaCandidate, _challenge: str) -> float:
    """Fraction of decoded words recognized as math vocabulary."""
    if candidate.strategy == "direct":
        return 0.6
    words = candidate.decoded_text.lower().split()
    if not words:
        return 0.0
    recognized = 0
    for w in words:
        if _is_number_word(w):
            recognized += 1
        elif w in _OPERATOR_WORDS or w in _CONTEXT_WORDS:
            recognized += 1
    return min(recognized / len(words), 1.0)


def _score_structural_conformity(candidate: CaptchaCandidate, _challenge: str) -> float:
    """Does expression follow captcha convention (A op B)?"""
    expr = candidate.expression
    if expr == "(direct)":
        return 0.6
    numbers = re.findall(r"\d+\.?\d*", expr)
    has_operator = bool(re.search(r"[+\-*/%]", expr))
    if len(numbers) == 2 and has_operator:
        return 1.0
    if len(numbers) == 1:
        return 0.3
    if len(numbers) >= 3:
        return 0.2
    return 0.0


class CaptchaChamber:
    """Multi-strategy self-experimenting captcha solver.

    Generate candidates → Score → Decide. No fallback chains. No API calls.
    Returns Optional[str]: answer with high confidence, or None to skip.
    """

    @classmethod
    def solve(cls, challenge_text: str) -> Optional[str]:
        """Solve challenge or return None if confidence too low.

        None means: "I don't know, skip this comment."
        This prevents wrong submissions that count toward a ban.
        """
        if not challenge_text or not challenge_text.strip():
            return None

        candidates: List[CaptchaCandidate] = []
        for strategy_fn in _STRATEGIES:
            try:
                results = strategy_fn(challenge_text)
                candidates.extend(results)
            except Exception as exc:
                logger.debug("Strategy %s failed: %s", strategy_fn.__name__, exc)

        if not candidates:
            return None

        for candidate in candidates:
            candidate.scores["expression"] = _score_expression(candidate, challenge_text)
            candidate.scores["consensus"] = _score_consensus(candidate, challenge_text, all_candidates=candidates)
            candidate.scores["range"] = _score_range(candidate, challenge_text)
            candidate.scores["completeness"] = _score_completeness(candidate, challenge_text)
            candidate.scores["decode_fidelity"] = _score_decode_fidelity(candidate, challenge_text)
            candidate.scores["structural_conformity"] = _score_structural_conformity(candidate, challenge_text)
            candidate.total_score = sum(candidate.scores.values())

        candidates.sort(key=lambda c: c.total_score, reverse=True)
        best = candidates[0]

        logger.debug(
            "CaptchaChamber: best=%s score=%.2f scores=%s strategies=%d",
            best.answer,
            best.total_score,
            best.scores,
            len(candidates),
        )

        if best.total_score < CONFIDENCE_THRESHOLD:
            logger.warning(
                "CaptchaChamber: low confidence (%.2f < %.2f), skipping. Best candidate: answer=%s strategy=%s",
                best.total_score,
                CONFIDENCE_THRESHOLD,
                best.answer,
                best.strategy,
            )
            return None

        return best.answer


# =============================================================================
# LLM FALLBACK — new code, off by default, only consulted on CaptchaChamber
# None (never as a double-check against a deterministic answer)
# =============================================================================

_LLM_ENABLED_FLAG = "VILLAGE_CHALLENGE_LLM_ENABLED"
_DEEPSEEK_API_KEY_VAR = "DEEPSEEK_API_KEY"
_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


def _deepseek_solve(challenge_text: str) -> Optional[str]:
    """Ask DeepSeek to solve a challenge the deterministic solver skipped.

    Only ever called when CaptchaChamber.solve() already returned None —
    see solve_and_verify() below. Returns None (not "0", not a guess) on
    any failure: missing key, network error, unparseable response. The
    ChallengeMonitor treats an LLM-fallback None exactly like a
    deterministic None — a recorded failure, not a submitted answer.
    """
    api_key = os.environ.get(_DEEPSEEK_API_KEY_VAR, "")
    if not api_key:
        logger.warning("_deepseek_solve: %s not set, cannot fall back", _DEEPSEEK_API_KEY_VAR)
        return None

    prompt = (
        "Solve this math word problem. The obfuscated text/casing is noise; "
        "the numbers and operation are what matter. Reply with ONLY the "
        "numeric answer, nothing else, to 2 decimal places (e.g. 42.00).\n\n"
        f"{challenge_text}"
    )
    body = json.dumps(
        {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": 20,
        }
    ).encode()
    req = urllib.request.Request(
        _DEEPSEEK_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("_deepseek_solve: API call failed: %s", exc)
        return None

    try:
        content = resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        logger.warning("_deepseek_solve: unexpected response shape: %r", resp)
        return None

    match = re.search(r"-?\d+\.?\d*", content)
    if not match:
        logger.warning("_deepseek_solve: no number found in response: %r", content)
        return None
    return match.group(0)


# =============================================================================
# TWO-STEP VERIFY FLOW — new code, adapted to the current live API contract
# (not ported — see module docstring for the discrepancy from source)
# =============================================================================


def solve_and_verify(mb_call, verification: dict) -> dict:
    """Solve a Moltbook verify challenge and submit it to POST /api/v1/verify.

    Args:
        mb_call: callable(path, method, body) -> dict, matching the `_mb()`
            helper already used in village/heartbeat.py. Not imported from
            there to keep this module standalone/testable.
        verification: the `verification` object as returned inline on a
            post/comment creation response — must contain `verification_code`
            and `challenge_text` (see agent-village/docs/BEFUND.md §3 for the
            exact response shape observed live).

    If CaptchaChamber.solve() returns None AND the VILLAGE_CHALLENGE_LLM_ENABLED
    env var is exactly "1", falls back to DeepSeek (requires DEEPSEEK_API_KEY).
    The deterministic solver always runs first and wins outright if it
    returns anything but None — the LLM is never a second opinion against a
    deterministic answer, only a fallback for the None case. Flag is off by
    default.

    Returns:
        dict with at least a `"solved"` bool. On low-confidence skip (both
        deterministic and, if enabled, LLM fallback returned None), returns
        {"solved": False, "skipped": True, "reason": "low_confidence"} and
        does NOT call the API — matching the "never guess" principle.
    """
    monitor = _challenge_monitor

    if monitor.is_halted:
        logger.error("solve_and_verify: refused — challenge monitor halted (too many failures)")
        return {"solved": False, "skipped": True, "reason": "monitor_halted", "stats": monitor.get_stats()}

    challenge_text = verification.get("challenge_text", "")
    verification_code = verification.get("verification_code", "")

    if not challenge_text or not verification_code:
        logger.error("solve_and_verify: verification object missing challenge_text/verification_code")
        return {"solved": False, "skipped": True, "reason": "malformed_verification"}

    monitor.check_format_change(challenge_text)

    answer = CaptchaChamber.solve(challenge_text)
    used_llm_fallback = False

    if answer is None and os.environ.get(_LLM_ENABLED_FLAG) == "1":
        logger.info("solve_and_verify: deterministic solver returned None, trying DeepSeek fallback")
        answer = _deepseek_solve(challenge_text)
        used_llm_fallback = answer is not None

    if answer is None:
        monitor.record_failure(challenge_text)
        logger.warning("solve_and_verify: low confidence, skipping. challenge=%r", challenge_text)
        return {"solved": False, "skipped": True, "reason": "low_confidence"}

    # Moltbook's documented answer format is "X.00" (two decimal places).
    answer_str = answer if "." in answer else f"{answer}.00"

    resp = mb_call("verify", "POST", {"verification_code": verification_code, "answer": answer_str})

    if isinstance(resp, dict) and resp.get("success"):
        monitor.record_success()
        logger.info(
            "solve_and_verify: solved (llm_fallback=%s). challenge=%r answer=%s",
            used_llm_fallback,
            challenge_text,
            answer_str,
        )
        return {"solved": True, "answer": answer_str, "used_llm_fallback": used_llm_fallback, "response": resp}

    monitor.record_failure(challenge_text)
    # FIX (docs/BEFUND.md §12): this used to log challenge_text[:60] —
    # these challenges are short (one sentence, ~100-250 chars), truncating
    # to 60 chars threw away exactly the part needed to debug a failure
    # after the fact (the challenge itself is single-use and expires
    # within minutes, so once logged-and-truncated the original is gone
    # for good). Log the full text; it's never excessively long.
    logger.warning(
        "solve_and_verify: verify call failed (llm_fallback=%s). challenge=%r answer=%s response=%r",
        used_llm_fallback,
        challenge_text,
        answer_str,
        resp,
    )
    return {
        "solved": False,
        "skipped": False,
        "reason": "verify_rejected",
        "answer": answer_str,
        "used_llm_fallback": used_llm_fallback,
        "response": resp,
    }
