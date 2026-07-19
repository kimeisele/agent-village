"""
Tests for scripts/operator_execute.py::resolve_target_file() -- the
path-validation gate closing a real exfiltration path found in Kim's
review of PR #16: `permissions: contents: read` stops this workflow
from writing to the repository, but does nothing to stop it from
reading arbitrary files elsewhere on the runner and sending their
content to DeepSeek. Unit-level tests against the function directly
(fast, no subprocess, no real API calls anywhere).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "operator_execute.py"


def _load_operator_execute_module():
    """scripts/ isn't a package -- load it directly by path so this test
    file doesn't need sys.path surgery affecting other tests."""
    spec = importlib.util.spec_from_file_location("operator_execute", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("operator_execute", module)
    spec.loader.exec_module(module)
    return module


oe = _load_operator_execute_module()


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal fake repository root with a real file, a .git/ dir, and
    an outside-the-repo sibling directory to traverse into."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "village").mkdir()
    (repo_root / "village" / "heartbeat.py").write_text("# fake heartbeat.py content\n")
    git_dir = repo_root / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\n")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-repo-content\n")

    evidence_path = repo_root / "evidence.json"

    return repo_root, outside, evidence_path


# 1) a valid repo file is accepted
def test_valid_repo_file_is_accepted(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    resolved = oe.resolve_target_file("village/heartbeat.py", repo_root, evidence_path)
    assert resolved == (repo_root / "village" / "heartbeat.py").resolve()


# 2) a missing file is rejected
def test_missing_file_is_rejected(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    with pytest.raises(oe.TargetPathError, match="not found"):
        oe.resolve_target_file("village/does_not_exist.py", repo_root, evidence_path)


# 3) ../../etc/passwd is rejected
def test_relative_traversal_outside_repo_is_rejected(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    with pytest.raises(oe.TargetPathError, match="outside the repository root"):
        oe.resolve_target_file("../../etc/passwd", repo_root, evidence_path)


def test_traversal_into_sibling_directory_is_rejected(fake_repo):
    repo_root, outside, evidence_path = fake_repo
    rel = f"../{outside.name}/secret.txt"
    with pytest.raises(oe.TargetPathError, match="outside the repository root"):
        oe.resolve_target_file(rel, repo_root, evidence_path)


# 4) an absolute path outside the repo is rejected
def test_absolute_path_outside_repo_is_rejected(fake_repo):
    repo_root, outside, evidence_path = fake_repo
    absolute = str((outside / "secret.txt").resolve())
    with pytest.raises(oe.TargetPathError, match="absolute path"):
        oe.resolve_target_file(absolute, repo_root, evidence_path)


def test_absolute_path_even_inside_repo_is_rejected(fake_repo):
    """Absolute paths are rejected on principle, regardless of where
    they'd resolve -- not just because they usually point outside."""
    repo_root, _outside, evidence_path = fake_repo
    absolute = str((repo_root / "village" / "heartbeat.py").resolve())
    with pytest.raises(oe.TargetPathError, match="absolute path"):
        oe.resolve_target_file(absolute, repo_root, evidence_path)


# 5) .git/config is rejected
def test_git_directory_contents_are_rejected(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    with pytest.raises(oe.TargetPathError, match=r"\.git/"):
        oe.resolve_target_file(".git/config", repo_root, evidence_path)


# 6) a symlink inside the repo pointing outside is rejected
def test_symlink_inside_repo_resolving_outside_is_rejected(fake_repo):
    repo_root, outside, evidence_path = fake_repo
    link = repo_root / "village" / "escape.py"
    link.symlink_to(outside / "secret.txt")
    with pytest.raises(oe.TargetPathError, match="outside the repository root"):
        oe.resolve_target_file("village/escape.py", repo_root, evidence_path)


def test_symlink_resolving_inside_repo_is_accepted(fake_repo):
    """Only symlinks resolving OUTSIDE the root are forbidden -- one
    that stays inside is fine (not one of Kim's listed rejections)."""
    repo_root, _outside, evidence_path = fake_repo
    link = repo_root / "village" / "alias.py"
    link.symlink_to(repo_root / "village" / "heartbeat.py")
    resolved = oe.resolve_target_file("village/alias.py", repo_root, evidence_path)
    assert resolved == (repo_root / "village" / "heartbeat.py").resolve()


# 7) a directory is rejected
def test_directory_is_rejected(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    with pytest.raises(oe.TargetPathError, match="directory"):
        oe.resolve_target_file("village", repo_root, evidence_path)


def test_evidence_output_file_itself_is_rejected(fake_repo):
    repo_root, _outside, evidence_path = fake_repo
    evidence_path.write_text("{}")
    rel = str(evidence_path.relative_to(repo_root))
    with pytest.raises(oe.TargetPathError, match="evidence output file"):
        oe.resolve_target_file(rel, repo_root, evidence_path)


# error messages never leak the full runner filesystem path
def test_error_messages_only_name_the_requested_relative_input(fake_repo):
    repo_root, outside, evidence_path = fake_repo
    absolute = str((outside / "secret.txt").resolve())
    with pytest.raises(oe.TargetPathError) as exc_info:
        oe.resolve_target_file(absolute, repo_root, evidence_path)
    message = str(exc_info.value)
    assert absolute in message  # the requested input itself is fine to echo back
    assert str(repo_root) not in message.replace(absolute, "")  # but no OTHER runner path leaks in

    with pytest.raises(oe.TargetPathError) as exc_info2:
        oe.resolve_target_file("../../etc/passwd", repo_root, evidence_path)
    message2 = str(exc_info2.value)
    assert message2 == "target_file resolves outside the repository root: '../../etc/passwd'"
    assert str(repo_root) not in message2  # the resolved absolute path is never named
