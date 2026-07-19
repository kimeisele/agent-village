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


def test_worker_module_does_not_import_heartbeat():
    """village/heartbeat.py owns bounty_complete(); the worker must not
    even be able to reach it via import, not just avoid calling it."""
    source = inspect.getsource(worker)
    assert "village.heartbeat" not in source
    assert "import heartbeat" not in source


def test_worker_module_has_no_subprocess_or_shell_execution():
    """No code path in the worker may execute anything the provider
    returns -- verified by absence of any shell/process/eval facility in
    the module's own source."""
    source = inspect.getsource(worker)
    for forbidden in ("subprocess", "os.system", "eval(", "exec(", "__import__"):
        assert forbidden not in source, f"found forbidden construct: {forbidden}"
