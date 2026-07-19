"""
Tests for village/moltbook_captcha.py.

Two independent test-case sets, per Kim's instruction:

1. TestChallengeSolver* / TestCaptchaChamber* classes: adapted from
   kimeisele/steward-protocol's vibe_core/mahamantra/tests/adapters/test_moltbook.py
   (commit 34a8a0efc25c15ef7c07dd4fb50aeb2510c071e8), covering arithmetic
   ChallengeSolver reported to Kim was covered in that suite
   (subtraction/multiplication/division/decimals/chained ops/word-fragment
   reassembly), plus the real "LOBSTER_CAPTCHA" fixture from that suite that
   directly exercises the aggressive/fuzzy strategy. One test from the source
   suite is NOT ported: test_basin_cosine_hkr_used_in_aggressive — it tests
   the RAMA phonetic module we deliberately did not pull in (see module
   docstring in moltbook_captcha.py and docs/BEFUND.md §5).

2. TestRealSamples: our own 5 live challenges captured against the real
   Moltbook API, recorded verbatim in docs/BEFUND.md §4.
"""

from __future__ import annotations

from village.moltbook_captcha import (
    CaptchaChamber,
    ChallengeSolver,
    _akshara_collapse,
    _collapse_all,
    _pada_collapse,
    _pada_exact,
    _varna_filter,
    solve_and_verify,
)

# =============================================================================
# 1. Adapted from steward-protocol's test_moltbook.py
# =============================================================================


class TestChallengeSolverArithmetic:
    def test_addition_with_digit(self):
        assert ChallengeSolver.solve("What is seven + 3?") == "10"

    def test_addition_with_word(self):
        assert ChallengeSolver.solve("Please add 5 and four") == "9"

    def test_subtraction_with_minus(self):
        assert ChallengeSolver.solve("What is ten - 3?") == "7"

    def test_subtraction_with_word(self):
        assert ChallengeSolver.solve("nine minus four is") == "5"

    def test_multiplication_with_times(self):
        assert ChallengeSolver.solve("four times five") == "20"

    def test_division_with_divided_by(self):
        assert ChallengeSolver.solve("What is twenty divided by 4?") == "5"

    def test_division_with_slash(self):
        assert ChallengeSolver.solve("100 / five") == "20"

    def test_division_by_zero_returns_zero(self):
        assert ChallengeSolver.solve("10 / zero") == "0"


class TestChallengeSolverRegression:
    """Real bugs from the source suite — must never recur."""

    def test_eighteen_not_corrupted(self):
        assert ChallengeSolver.solve("What is eighteen + 2?") == "20"

    def test_eighteen_minus_eight(self):
        assert ChallengeSolver.solve("What is eighteen - eight?") == "10"

    def test_eighty_not_corrupted(self):
        assert ChallengeSolver.solve("What is eighty + twenty?") == "100"

    def test_thirteen_not_corrupted(self):
        assert ChallengeSolver.solve("What is thirteen + seven?") == "20"

    def test_nineteen_not_corrupted(self):
        assert ChallengeSolver.solve("What is nineteen - nine?") == "10"

    def test_ninety_not_corrupted(self):
        assert ChallengeSolver.solve("What is ninety - fifty?") == "40"


class TestChallengeSolverAdvanced:
    def test_decimal_addition(self):
        assert ChallengeSolver.solve("What is 3.5 + 1.5?") == "5"

    def test_chained_operations(self):
        assert ChallengeSolver.solve("What is 10 + 5 - 3?") == "12"

    def test_operator_precedence(self):
        assert ChallengeSolver.solve("What is 2 + 3 * 4?") == "14"

    def test_parentheses(self):
        assert ChallengeSolver.solve("What is (5 + 3) * 2?") == "16"

    def test_modulo(self):
        assert ChallengeSolver.solve("What is 17 modulo 5?") == "2"

    def test_power(self):
        assert ChallengeSolver.solve("What is 2 raised to power of 3?") == "8"

    def test_hyphenated_twenty_three(self):
        assert ChallengeSolver.solve("What is twenty-three plus five?") == "28"

    def test_hyphenated_ninety_nine(self):
        assert ChallengeSolver.solve("What is ninety-nine minus seventy-seven?") == "22"

    def test_division_result_integer(self):
        assert ChallengeSolver.solve("What is 100 divided by 4?") == "25"


class TestChallengeSolverProperties:
    def test_always_returns_string(self):
        for inp in ["What is seven + 3?", "Just nonsense here", "", "42", "a + b"]:
            assert isinstance(ChallengeSolver.solve(inp), str)

    def test_insufficient_numbers_returns_zero(self):
        assert ChallengeSolver.solve("What is the meaning of life?") == "0"
        assert ChallengeSolver.solve("") == "0"

    def test_single_number_returns_itself(self):
        assert ChallengeSolver.solve("Just the number 5") == "5"

    def test_word_map_has_no_duplicates(self):
        words = [w for w, _ in ChallengeSolver.WORD_MAP]
        assert len(words) == len(set(words))

    def test_compound_numbers_listed_before_substrings(self):
        word_list = [w for w, _ in ChallengeSolver.WORD_MAP]
        assert word_list.index("eighteen") < word_list.index("eight")
        assert word_list.index("eighty") < word_list.index("eight")
        assert word_list.index("nineteen") < word_list.index("nine")
        assert word_list.index("ninety") < word_list.index("nine")


class TestCaptchaChamberSolve:
    LOBSTER_CAPTCHA = (
        "A] LoB-StEr~ ClAwS^ ArE lIkE Um, lOoObsssTer sWiMmS| AnD] "
        "tHe ClAwFoRcE Is T wE nT y T hR eE NoOtOnS~ AnD] tHe OtHeR "
        "ClAw Is F oU r NoOtOnS, W hAt I s T oTaL F oR cE?"
    )

    def test_lobster_captcha_solves(self):
        """Real captcha from source suite: 23 + 4 = 27. Exercises the
        aggressive/fuzzy strategy — this is the one that changed metric
        (RAMA -> difflib). Passing here is the key regression check."""
        assert CaptchaChamber.solve(self.LOBSTER_CAPTCHA) == "27"

    def test_simple_addition(self):
        assert CaptchaChamber.solve("What is seven + 3?") == "10"

    def test_simple_subtraction(self):
        assert CaptchaChamber.solve("What is ten - 3?") == "7"

    def test_simple_multiplication(self):
        assert CaptchaChamber.solve("What is two * 3?") == "6"

    def test_simple_division(self):
        assert CaptchaChamber.solve("What is twenty divided by 4?") == "5"

    def test_hyphenated_compound(self):
        assert CaptchaChamber.solve("What is twenty-three plus five?") == "28"


class TestCaptchaChamberConfidence:
    def test_empty_returns_none(self):
        assert CaptchaChamber.solve("") is None

    def test_nonsense_returns_none(self):
        assert CaptchaChamber.solve("Just some random text") is None

    def test_non_math_captcha_returns_none(self):
        assert CaptchaChamber.solve("What color is the sky?") is None


class TestCaptchaChamberPipeline:
    def test_varna_filter(self):
        filtered = _varna_filter("A] LoB-StEr~ ClAwS^")
        assert "]" not in filtered
        assert "~" not in filtered
        assert "lob" in filtered
        assert filtered == filtered.lower()

    def test_akshara_collapse(self):
        assert _akshara_collapse("looobssster") == "lobster"
        assert _akshara_collapse("aaabbbccc") == "abc"

    def test_collapse_all(self):
        assert _collapse_all("foorty") == "forty"
        assert _collapse_all("tweeenntty") == "twenty"
        assert _collapse_all("") == ""

    def test_pada_exact_joins_fragments(self):
        assert "twenty" in _pada_exact(["t", "we", "nt", "y"])
        assert "three" in _pada_exact(["t", "hr", "ee"])

    def test_pada_collapse_handles_duplicates(self):
        result = _pada_collapse(["min", "u", "s", "s", "e", "ven"])
        assert "minus" in result
        assert "seven" in result


# =============================================================================
# 2. Our own 5 live samples — docs/BEFUND.md §4, captured against the real
#    Moltbook API on throwaway posts/comments, deleted after capture.
# =============================================================================


class TestRealSamples:
    def test_sample_1_post(self):
        """40 = 25 + 15."""
        text = (
            "A] lOoObBsStTeErR ]sW/iMmS [iN tHe ]coOl WaTeR, Um] cLaW F oR cE "
            "Is/ tWeNtY ]fIvE {nEeWtOoNs, PlUs} FiFfTeEeN <nOoToNs> - hOw] "
            "mUcH ToTaL FoR cE^?"
        )
        assert CaptchaChamber.solve(text) == "40"

    def test_sample_2_comment(self):
        """28 = 23 + 5."""
        text = (
            "A] LoBbEr'S ClA w ExE rTs TwEnTy ThReE NeWtOnS ^ AnD An TeNnA "
            "ToUcH AdD s FiV e NeWtOnS ~, Um LlOoObBsStEeR PhYySxIcS Lo.oBb "
            "St Er, WhAt Is ToTaL FoR cE?"
        )
        assert CaptchaChamber.solve(text) == "28"

    def test_sample_3_post(self):
        """32 = 27 + 5 (velocity)."""
        text = (
            "A] lO b-StEr SwIm S aT tW/eN tY sE vEn CeN tI mEt ErS PeR Se Co "
            "Nd - AnD^ aCcEeLeR aTeS bY[ fIiV e, WhAtS tHe NeW VeLoOciTyYY?"
        )
        assert CaptchaChamber.solve(text) == "32"

    def test_sample_4_post(self):
        """57 = 35 + 22."""
        text = (
            "A] Lo.OoBbSsStTeErR- ClAw^ FoOrRcCeE ThIrTy FiVe NeWToNs ~ "
            "DuRiNg DoMiNaNcE FiGhT, AnD ] AnOtHeR Lo.oBbSsStTeErR- ClAw^ "
            "TwEnTy TwO NeWToNs, WhAt Is ThE ToTaL FoRcE?"
        )
        assert CaptchaChamber.solve(text) == "57"

    def test_sample_5_post(self):
        """30 = 23 + 7 (velocity)."""
        text = (
            "Lo]oBbSsTtEeR S^wIiMmS/ aT tW/eNnTy ThReE {mEeTtEeR}s PeR "
            "sEeCcOoNnD ~aNd/ GgAaIiNnSs {SsEeVvEeN} mEeTtEeR}s PeR "
            "sEeCcOoNnD, WwHhAaTt'S TtHhEe NnEeWw VvEeLlOoOcCiItTy?"
        )
        assert CaptchaChamber.solve(text) == "30"

    def test_sample_6_live_e2e_subtraction(self):
        """From the live automated E2E test run (docs/BEFUND.md §5 addendum):
        the solver answered 28.00 here and the API rejected it. Correct
        answer (own calculation, not API-confirmed): 42 - 12 = 30."""
        text = (
            "A] lOoObbsssTeR rAnS/ liKe- a bIt^ oFf- tHe Er?gG Um mMm hAs^ "
            "cLaW fO rCe- oF/ fOrTy T wo] nEeWtoNs- BuT^ iT- looses[ "
            "tWeLve] nEeWtoNs, HoW/ mAnY- nOw<?"
        )
        assert CaptchaChamber.solve(text) == "30"


# =============================================================================
# 3. solve_and_verify() — the new two-step HTTP flow, mocked
# =============================================================================


class TestSolveAndVerify:
    def test_solves_and_calls_verify_endpoint(self):
        calls = []

        def fake_mb(path, method, body):
            calls.append((path, method, body))
            assert path == "verify"
            assert method == "POST"
            assert body["verification_code"] == "moltbook_verify_test123"
            return {"success": True, "message": "Verification successful!"}

        verification = {
            "verification_code": "moltbook_verify_test123",
            "challenge_text": "What is seven + 3?",
        }
        result = solve_and_verify(fake_mb, verification)
        assert result["solved"] is True
        assert result["answer"] == "10.00"
        assert len(calls) == 1

    def test_low_confidence_skips_without_calling_api(self):
        calls = []

        def fake_mb(path, method, body):
            calls.append((path, method, body))
            return {"success": True}

        verification = {
            "verification_code": "moltbook_verify_test456",
            "challenge_text": "What color is the sky?",
        }
        result = solve_and_verify(fake_mb, verification)
        assert result["solved"] is False
        assert result["skipped"] is True
        assert result["reason"] == "low_confidence"
        assert len(calls) == 0  # never called the API — "never guess" principle

    def test_malformed_verification_object(self):
        result = solve_and_verify(lambda *a: {}, {})
        assert result["solved"] is False
        assert result["reason"] == "malformed_verification"


# =============================================================================
# 4. LLM fallback — mocked, no real network call. Flag off by default.
# =============================================================================


class TestLlmFallback:
    def test_disabled_by_default_stays_low_confidence(self, monkeypatch):
        """Flag unset -> deterministic None must NOT trigger the fallback,
        even if DEEPSEEK_API_KEY happens to be set."""
        monkeypatch.delenv("VILLAGE_CHALLENGE_LLM_ENABLED", raising=False)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-in-this-test")
        calls = []

        def fake_mb(path, method, body):
            calls.append((path, method, body))
            return {"success": True}

        verification = {"verification_code": "x", "challenge_text": "What color is the sky?"}
        result = solve_and_verify(fake_mb, verification)
        assert result["reason"] == "low_confidence"
        assert len(calls) == 0

    def test_enabled_but_no_api_key_stays_low_confidence(self, monkeypatch):
        monkeypatch.setenv("VILLAGE_CHALLENGE_LLM_ENABLED", "1")
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        verification = {"verification_code": "x", "challenge_text": "What color is the sky?"}
        result = solve_and_verify(lambda *a: {"success": True}, verification)
        assert result["reason"] == "low_confidence"

    def test_deterministic_answer_never_calls_llm(self, monkeypatch):
        """Deterministic solver succeeds -> _deepseek_solve must not even be
        imported/called, flag or no flag."""
        import village.moltbook_captcha as mc

        monkeypatch.setenv("VILLAGE_CHALLENGE_LLM_ENABLED", "1")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-in-this-test")

        def boom(_challenge_text):
            raise AssertionError("_deepseek_solve must not be called when deterministic solver succeeds")

        monkeypatch.setattr(mc, "_deepseek_solve", boom)
        verification = {"verification_code": "x", "challenge_text": "What is seven + 3?"}
        result = solve_and_verify(lambda *a: {"success": True}, verification)
        assert result["solved"] is True
        assert result["used_llm_fallback"] is False

    def test_llm_fallback_used_when_deterministic_returns_none(self, monkeypatch):
        import village.moltbook_captcha as mc

        monkeypatch.setenv("VILLAGE_CHALLENGE_LLM_ENABLED", "1")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-in-this-test")
        monkeypatch.setattr(mc, "_deepseek_solve", lambda challenge_text: "99")

        calls = []

        def fake_mb(path, method, body):
            calls.append(body)
            return {"success": True}

        verification = {"verification_code": "x", "challenge_text": "What color is the sky?"}
        result = solve_and_verify(fake_mb, verification)
        assert result["solved"] is True
        assert result["used_llm_fallback"] is True
        assert result["answer"] == "99.00"
        assert calls[0]["answer"] == "99.00"

    def test_llm_fallback_also_none_stays_low_confidence(self, monkeypatch):
        import village.moltbook_captcha as mc

        monkeypatch.setenv("VILLAGE_CHALLENGE_LLM_ENABLED", "1")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "unused-in-this-test")
        monkeypatch.setattr(mc, "_deepseek_solve", lambda challenge_text: None)

        calls = []
        result = solve_and_verify(lambda *a: calls.append(a) or {"success": True}, {"verification_code": "x", "challenge_text": "gibberish"})
        assert result["reason"] == "low_confidence"
        assert len(calls) == 0
