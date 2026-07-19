"""
Regression test for docs/research/BOUNTY_REVIEW_GATE_01.md Blocker 3:
village.heartbeat._save() writes to a temp file and atomically replaces
the target, so a write failure mid-save never leaves a truncated/corrupt
JSON file behind -- the original file (or its absence) is preserved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import village.heartbeat as hb


def test_save_write_failure_leaves_original_file_unchanged(tmp_path, monkeypatch):
    target = tmp_path / "data.json"
    hb._save(target, {"a": 1})
    original = target.read_text()

    def failing_write_text(self, *args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with pytest.raises(OSError):
        hb._save(target, {"a": 2})
    monkeypatch.undo()  # restore Path.write_text before reading the file back

    assert target.read_text() == original
    assert hb._load(target) == {"a": 1}


def test_save_write_failure_on_nonexistent_target_leaves_no_file(tmp_path, monkeypatch):
    target = tmp_path / "new.json"
    assert not target.exists()

    def failing_write_text(self, *args, **kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "write_text", failing_write_text)
    with pytest.raises(OSError):
        hb._save(target, {"a": 1})
    monkeypatch.undo()

    assert not target.exists()  # no half-written file left behind


def test_save_does_not_leave_stray_tmp_files_on_success(tmp_path):
    target = tmp_path / "data.json"
    hb._save(target, {"a": 1})

    leftovers = list(tmp_path.glob("*.tmp*"))
    assert leftovers == []


def test_save_round_trips_normally(tmp_path):
    target = tmp_path / "data.json"
    hb._save(target, {"a": 1, "b": [1, 2, 3]})
    assert hb._load(target) == {"a": 1, "b": [1, 2, 3]}
