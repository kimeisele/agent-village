"""
Tests for scripts/worker_proof_01.py -- the actual driver invoked by
.github/workflows/worker-proof-01.yml. Run as a subprocess (it's a
script, not a package module) with the real environment cleared of
DEEPSEEK_API_KEY, so this only ever exercises the clean-skip path --
never a real network call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "worker_proof_01.py"


def test_missing_secret_skips_cleanly_with_nonzero_exit(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    env = dict(os.environ)
    env.pop("DEEPSEEK_API_KEY", None)
    env["WORKER_PROOF_EVIDENCE_PATH"] = str(evidence_path)

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
    # Never a fake success.
    assert evidence["status"] != "SUCCEEDED"


def test_missing_target_file_fails_cleanly(tmp_path):
    evidence_path = tmp_path / "evidence.json"
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = "sk-fake-for-this-test-only"
    env["WORKER_PROOF_EVIDENCE_PATH"] = str(evidence_path)
    env["WORKER_PROOF_TARGET_FILE"] = "village/this_file_does_not_exist.py"

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
    assert not evidence_path.exists()  # fails before ever writing evidence
