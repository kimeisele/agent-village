"""
Tests for scripts/operator_execute.py -- the actual driver invoked by
.github/workflows/operator-execute-01.yml. Run as a subprocess with the
real environment cleared of DEEPSEEK_API_KEY, so this only ever
exercises the clean-skip path -- never a real network call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "operator_execute.py"


def test_missing_secret_skips_cleanly_with_nonzero_exit(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env["OPERATOR_EVIDENCE_PATH"] = str(evidence_path)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1
    assert "not set" in proc.stderr or "not set" in proc.stdout
    evidence = json.loads(evidence_path.read_text())
    assert evidence["status"] == "SKIPPED_NO_SECRET"


def test_missing_target_file_fails_cleanly(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-fake-for-this-test-only"
    env["OPERATOR_EVIDENCE_PATH"] = str(evidence_path)
    env["OPERATOR_TARGET_FILE"] = "village/this_file_does_not_exist.py"

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1
    assert "target file not found" in proc.stderr or "target file not found" in proc.stdout
    assert not evidence_path.exists()


def test_explicit_bounty_id_not_found_fails_cleanly(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-fake-for-this-test-only"
    env["OPERATOR_EVIDENCE_PATH"] = str(evidence_path)
    env["OPERATOR_BOUNTY_ID"] = "b-does-not-exist"

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 1
    assert "not found" in proc.stderr or "not found" in proc.stdout
