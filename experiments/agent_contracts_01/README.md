# Experiment: `ai-agent-contracts` for Gap 3 (bounty governance)

Isolated evaluation experiment. **Not production code.** See
`docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md` for the full write-up,
findings, comparison, and final decision.

Pinned to `ai-agent-contracts==0.3.2` (PyPI) == GitHub tag `v0.3.2` ==
commit `273d9e02e136759445ea18e2fda94ffd609f0221` in
`flyersworder/agent-contracts`. Requires Python **3.12+** — this repo's
own interpreter is 3.11, so this experiment needs its own venv on a
newer interpreter, kept entirely local to this folder.

## Running it

```bash
cd experiments/agent_contracts_01
python3.12 -m venv .venv   # or any 3.12+/3.13 interpreter
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest tests/ -v
```

## Contents

- `contract_experiment.py` — models the real `b001` bounty
  (`data/village/bounties.json`) as an `agent_contracts.Contract`.
- `stdlib_baseline.py` — minimal stdlib-only equivalent, same scenario,
  for direct comparison.
- `tests/test_agent_contracts_experiment.py` — the 6 required test cases
  plus one exploratory multi-agent-delegation check, against the real
  library.
- `tests/test_stdlib_baseline.py` — the same 6 cases against the stdlib
  baseline.

Not referenced by any file outside this directory. `village_core.py`,
`heartbeat.py`, `brain.py`, `moltbook_captcha.py`, and
`.github/workflows/*` are unchanged by this experiment.
