# Design note — agentis-colonies' confidence ladder mapped onto Gap 5

Status: **design note, no code.** `Replikanti/agentis-colonies` is
categorized LEARN in `docs/research/CAPABILITY_SURVEY_01.md` — its
actual runtime (`Replikanti/agentis`) is admitted-proprietary by the
repo's own README, so nothing here is DEPEND/VENDOR-able. Only the
documented *pattern* (the four-tier ladder in
`doc/adr/ADR-0001-confidence-tiers.md`, per the survey) is usable, as a
reference, per the shortlist entry.

## What agentis-colonies actually does (per CAPABILITY_SURVEY_01 §3)

A four-tier confidence ladder — `shadow → propose → review-gated →
autonomous` — that agents climb "based on measured experience, not
hand-tuned thresholds." Higher tiers grant more autonomy; movement
between tiers is evidence-driven, not manually assigned.

## Mapping onto Agent Village's actual reputation state

Current code (`village/heartbeat.py::derive()`,
`data/village/pokedex.json`):

```python
"status": "observed"  # always — no code path ever writes anything else
```

`ARCHITECTURE_VISION.md` §5 names a four-rung ladder —
`OBSERVED → CLAIMED → VERIFIED → RESIDENT` — but it has been aspirational
text only since it was written; this is Gap 5 exactly.

## Smallest defensible extension: one real second tier

Not a full four-tier system in one slice. The evidence-driven-progression
*idea* from agentis-colonies is the useful part, not its specific
four-tier vocabulary (which is about autonomy/execution rights — Agent
Village grants agents no execution rights at all today, so "shadow →
autonomous" doesn't map cleanly). What does map is the **mechanism**:
promotion should be triggered by a measured event, not a manual flag.

Proposed smallest step: `OBSERVED → CLAIMED`.

- **OBSERVED** (current, unchanged): registered via a Moltbook
  comment/GitHub issue, name-based, unauthenticated (SPEC.md §2.1 "Known
  limitation" — still true).
- **CLAIMED** (new): the same actor_id is seen again in a second,
  independent, successfully-processed interaction (e.g. a verified
  bounty claim, per `village/heartbeat.py::bounty_claim()`, or a second
  registration-adjacent comment with the same actor_id after migration,
  `village_core.migrate_pokedex()`/§C.1). "Measured experience," in
  agentis-colonies' phrase, translated into something Agent Village can
  actually observe today: *a second real, verified event from the same
  actor_id*, not a hand-set flag.

## What would need original implementation (not from agentis-colonies — nothing there is copyable)

1. A promotion check: on any successful `dex_register()`/bounty-claim
   confirmation, look up whether this `actor_id` already has a prior
   pokedex entry with `status: "observed"` and at least one other
   confirmed interaction on record; if so, promote to `"claimed"`.
2. A migration path for the field itself (same pattern already used for
   `actor_id` in `village_core.migrate_pokedex()`, SPEC.md §C.1) — old
   entries default to `"observed"`, nothing breaks.
3. A test proving the *opposite* also holds: a single interaction never
   promotes (guards against a trivial off-by-one that would make
   `CLAIMED` meaningless).

## What this deliberately does NOT do

- No autonomy/execution-rights change at any tier — Agent Village grants
  no execution rights today (SPEC.md §A.5), and nothing here proposes
  changing that. agentis-colonies' tiers are about autonomy; Agent
  Village's tiers (if built) would only be about observed trust signal,
  a narrower and safer claim.
- No `VERIFIED`/`RESIDENT` tiers designed here — undefined until
  `OBSERVED → CLAIMED` exists and is observed to behave sensibly.
- No connection to `village/contracts.py` proposed here, unlike the
  nightforge note — reputation tier and contract governance are
  different concerns (SPEC.md §A.11 lists "reputation/settlement" as a
  distinct, later Marketplace component, not a Contract field).

## Conclusion

No code, no PR follows from this note by design (per the
CAPABILITY_SURVEY_01 shortlist entry: "written proposal, no code"). The
concrete next step, if and when Kim wants to pursue Gap 5, is a small,
additive `pokedex.json` schema change (`OBSERVED → CLAIMED` only) plus a
migration test — not agentis-colonies' four-tier vocabulary, which
doesn't fit Agent Village's actual (execution-rights-free) architecture.
