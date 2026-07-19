"""
Enforces, structurally, that village/worker.py can never fulfill a
contract or complete a bounty -- the single hardest requirement of the
LLM-execution slice (SPEC.md §A.5). Not a behavioral test of what the
worker happens to call at runtime; a source-level guarantee that the
capability doesn't exist in the module at all, so it can't silently
regress via a future refactor that "just this once" adds a shortcut.
"""

from __future__ import annotations

import ast
import inspect

import village.execution_orchestrator as execution_orchestrator
import village.interpreter as interpreter
import village.worker as worker


def _call_names(source: str) -> set[str]:
    """All function/method names actually CALLED in the module's code --
    ignores docstrings/comments entirely (this module's own docstrings
    legitimately explain, in prose, that fulfill()/bounty_complete() are
    never called, which a naive substring check would false-positive
    on)."""
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_worker_source_never_calls_fulfill_or_bounty_complete():
    calls = _call_names(inspect.getsource(worker))
    assert "fulfill" not in calls
    assert "bounty_complete" not in calls


def test_worker_source_never_calls_bounty_submit_or_bounty_review():
    """docs/research/BOUNTY_REVIEW_GATE_01.md: the review gate adds two
    more authority-carrying functions (bounty_submit/bounty_review) --
    the worker must not be able to reach either. bounty_submit() is a
    legitimate thing SOME orchestration layer calls with a worker's
    WorkResult, but that orchestration explicitly lives outside
    village/worker.py/village/interpreter.py, never inside them."""
    calls = _call_names(inspect.getsource(worker))
    assert "bounty_submit" not in calls
    assert "bounty_review" not in calls


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_worker_module_does_not_import_heartbeat():
    """village/heartbeat.py owns bounty_complete(); the worker must not
    even be able to reach it via import, not just avoid calling it. AST
    import-node check, not a substring grep -- this module's own
    docstring explains the guarantee in prose (mentioning
    "village.heartbeat" by name), which a substring check would
    false-positive on."""
    imports = _imported_module_names(inspect.getsource(worker))
    assert "village.heartbeat" not in imports
    assert not any(m.startswith("village.heartbeat") for m in imports)


def test_worker_module_does_not_import_bounty_review():
    """village/bounty_review.py owns bounty_submit()/bounty_review();
    the worker must not be able to reach either via import."""
    imports = _imported_module_names(inspect.getsource(worker))
    assert not any(m.startswith("village.bounty_review") for m in imports)


def test_interpreter_module_does_not_import_heartbeat_or_bounty_review():
    imports = _imported_module_names(inspect.getsource(interpreter))
    assert not any(m.startswith("village.heartbeat") for m in imports)
    assert not any(m.startswith("village.bounty_review") for m in imports)


def test_worker_module_has_no_subprocess_or_shell_execution():
    """No code path in the worker may execute anything the provider
    returns -- verified by absence of any shell/process/eval facility in
    the module's own source."""
    source = inspect.getsource(worker)
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "__import__"):
        assert forbidden not in source, f"found forbidden construct: {forbidden}"


def test_interpreter_module_also_never_calls_fulfill_or_bounty_complete():
    """v2 adds a second LLM call site (village/interpreter.py's
    build_interpretation_prompt is invoked from worker.py) -- the
    boundary must hold there too, not just in worker.py."""
    calls = _call_names(inspect.getsource(interpreter))
    assert "fulfill" not in calls
    assert "bounty_complete" not in calls


def test_interpreter_module_also_never_calls_bounty_submit_or_bounty_review():
    calls = _call_names(inspect.getsource(interpreter))
    assert "bounty_submit" not in calls
    assert "bounty_review" not in calls


def test_interpreter_module_has_no_subprocess_or_shell_execution():
    source = inspect.getsource(interpreter)
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "__import__"):
        assert forbidden not in source, f"found forbidden construct: {forbidden}"


def test_max_llm_calls_per_execution_is_a_small_fixed_constant():
    """The new hard limit this slice adds (not weakening PR #13's
    guarantees, adding a new one): a repair loop must have a fixed,
    named, small ceiling -- not "unbounded because budget allows it"."""
    assert worker.MAX_LLM_CALLS_PER_EXECUTION == 4
    assert worker.MAX_REPAIR_ATTEMPTS == 2
    assert worker.MAX_LLM_CALLS_PER_EXECUTION < 10  # sanity: "small", not just "finite"


# =============================================================================
# docs/research/OPERATOR_EXECUTION_01.md: village/execution_orchestrator.py
# may run the worker and call bounty_submit() -- it must never be able to
# reach bounty_review()/bounty_complete()/contract.fulfill(), and neither
# worker.py nor interpreter.py may reach the orchestrator itself.
# =============================================================================


def test_worker_module_does_not_import_execution_orchestrator():
    imports = _imported_module_names(inspect.getsource(worker))
    assert not any(m.startswith("village.execution_orchestrator") for m in imports)


def test_worker_source_never_calls_run_operator_execution():
    calls = _call_names(inspect.getsource(worker))
    assert "run_operator_execution" not in calls


def test_interpreter_module_does_not_import_execution_orchestrator():
    imports = _imported_module_names(inspect.getsource(interpreter))
    assert not any(m.startswith("village.execution_orchestrator") for m in imports)


def test_interpreter_source_never_calls_run_operator_execution():
    calls = _call_names(inspect.getsource(interpreter))
    assert "run_operator_execution" not in calls


def test_orchestrator_never_calls_review_fulfill_or_complete():
    """The orchestrator MAY call bounty_submit() (that's its whole job)
    but must never call bounty_review(), bounty_complete(), or
    .fulfill() -- submitted is as far as it goes."""
    calls = _call_names(inspect.getsource(execution_orchestrator))
    assert "bounty_review" not in calls
    assert "bounty_complete" not in calls
    assert "fulfill" not in calls
    assert "bounty_submit" in calls  # sanity: it DOES call this, that's the point


def test_orchestrator_has_no_subprocess_or_shell_execution():
    source = inspect.getsource(execution_orchestrator)
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "__import__"):
        assert forbidden not in source, f"found forbidden construct: {forbidden}"


def test_orchestrator_does_not_import_git_or_push_facilities():
    """No commit/push/PR/merge capability anywhere in the orchestrator's
    own source -- it only ever reads/writes local JSON state files via
    the existing village.heartbeat/village.bounty_review helpers."""
    source = inspect.getsource(execution_orchestrator)
    for forbidden in ("git ", "gh pr", "gh api", "requests.post", "urllib.request"):
        assert forbidden not in source, f"found forbidden construct: {forbidden}"
