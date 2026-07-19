"""
Tests for actor-ID-keyed pokedex identity (docs/SPEC.md §C.1, §E.1-§E.3).

Covers the concrete bug named in SPEC.md §C.1: dex_register() used to key
purely by display `name`, so two different platform actors who happened to
pick the same name collided into one pokedex entry, and a legitimate
display-name change for the same actor created a second entry instead of
updating the first.
"""

from __future__ import annotations

import village.heartbeat as hb
from village.village_core import legacy_actor_id, migrate_pokedex


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "POKEDEX", tmp_path / "pokedex.json")


# =============================================================================
# §E.1 — distinct actor_ids sharing a display name stay separate
# =============================================================================


def test_two_actor_ids_same_display_name_get_separate_entries(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    a = hb.dex_register("Lobster", actor_id="platform:1")
    b = hb.dex_register("Lobster", actor_id="platform:2")

    assert a.get("_dup") is None
    assert b.get("_dup") is None
    agents = hb.dex_list()
    assert len(agents) == 2
    assert {ag["actor_id"] for ag in agents} == {"platform:1", "platform:2"}
    assert all(ag["name"] == "Lobster" for ag in agents)


# =============================================================================
# §E.2 — a display-name change for the same actor_id preserves identity
# =============================================================================


def test_same_actor_id_new_name_updates_in_place_not_duplicated(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    first = hb.dex_register("OldName", actor_id="platform:42")
    second = hb.dex_register("NewName", actor_id="platform:42")

    agents = hb.dex_list()
    assert len(agents) == 1  # not duplicated
    assert agents[0]["name"] == "NewName"  # updated in place
    assert second.get("_dup") is True
    assert first["seed"] == hb.derive("OldName")["seed"]  # sanity: distinct derive() calls


# =============================================================================
# §E.3 — existing (pre-actor_id) pokedex entries survive migration, no data loss
# =============================================================================


def test_migrate_pokedex_adds_legacy_actor_id_without_dropping_fields():
    # Exact shape of the real repo's data/village/pokedex.json entry
    # (B_ClawAssistant, Proof 1) as it existed before this slice.
    dex = {
        "agents": [
            {
                "name": "B_ClawAssistant",
                "status": "observed",
                "element": "prithvi",
                "zone": "engineering",
                "guardian": "prahlada",
                "guna": "SATTVA",
                "seed": 1506,
                "registered_at": 1784406096.094726,
            }
        ],
        "total": 1,
    }
    migrated, changed = migrate_pokedex(dex)

    assert changed is True
    assert len(migrated["agents"]) == 1
    entry = migrated["agents"][0]
    assert entry["actor_id"] == legacy_actor_id("B_ClawAssistant")
    # Every original field is untouched.
    assert entry["name"] == "B_ClawAssistant"
    assert entry["status"] == "observed"
    assert entry["element"] == "prithvi"
    assert entry["zone"] == "engineering"
    assert entry["guardian"] == "prahlada"
    assert entry["guna"] == "SATTVA"
    assert entry["seed"] == 1506
    assert entry["registered_at"] == 1784406096.094726
    assert migrated["total"] == 1


def test_migrate_pokedex_is_idempotent():
    dex = {"agents": [{"name": "X", "actor_id": "platform:9"}], "total": 1}
    migrated_once, changed_once = migrate_pokedex(dex)
    migrated_twice, changed_twice = migrate_pokedex(migrated_once)
    assert changed_once is False  # already had actor_id, nothing to do
    assert changed_twice is False
    assert migrated_once == migrated_twice


def test_dex_register_migrates_real_pre_actor_id_pokedex_on_load(monkeypatch, tmp_path):
    """End-to-end: a real pre-C.1 pokedex.json on disk (no actor_id) is
    readable and gets migrated the moment any dex_* function touches it —
    no manual migration step required, no data lost."""
    _setup(monkeypatch, tmp_path)
    hb._save(
        hb.POKEDEX,
        {
            "agents": [
                {
                    "name": "B_ClawAssistant",
                    "status": "observed",
                    "element": "prithvi",
                    "zone": "engineering",
                    "guardian": "prahlada",
                    "guna": "SATTVA",
                    "seed": 1506,
                    "registered_at": 1784406096.094726,
                }
            ],
            "total": 1,
        },
    )

    agents = hb.dex_list()
    assert len(agents) == 1
    assert agents[0]["actor_id"] == legacy_actor_id("B_ClawAssistant")
    assert agents[0]["name"] == "B_ClawAssistant"

    # A fresh registration under the SAME actor_id (re-derived the same
    # way a real second sighting of this exact legacy agent would be,
    # since no real actor_id was ever known for it) must update, not
    # duplicate.
    hb.dex_register("B_ClawAssistant", actor_id=legacy_actor_id("B_ClawAssistant"))
    assert len(hb.dex_list()) == 1

    # A DIFFERENT actor_id with the same name must NOT collide with the
    # migrated legacy entry.
    hb.dex_register("B_ClawAssistant", actor_id="platform:real-id-999")
    assert len(hb.dex_list()) == 2
