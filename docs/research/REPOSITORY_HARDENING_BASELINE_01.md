# Repository Hardening Baseline 01 — pure recon, no settings changed

Status: read-only survey via `gh api`, requested alongside `docs/
research/OPERATOR_EXECUTION_01.md`. **No repository setting was
changed to produce this document.** No ruleset activated, no branch
protection enabled, nothing that could lock Kim or the Builder out.

## Default branch

`main` (`repos/kimeisele/agent-village` → `default_branch: "main"`).

## Branch protection

`repos/kimeisele/agent-village/branches/main` → `"protected": false`,
`"protection": {"enabled": false, ...}`. Confirmed with a direct query
to the protection endpoint itself: `404 "Branch not protected"`.

**`main` currently has no branch protection at all.** Concretely, this
means (all default GitHub behavior for an unprotected branch, not a
guess):
- Direct pushes to `main` are possible for anyone with write access
  (currently: `kimeisele` only, see Collaborators below) -- nothing in
  GitHub itself has enforced the "PR + review" workflow this project has
  followed by convention across every slice in this thread. It has been
  a discipline, not a platform-enforced rule.
- Force-push to `main` is not blocked by GitHub.
- Branch deletion is not blocked by GitHub.
- No required status checks exist at the platform level -- CI (`tests.
  yml`) has been green on every merged PR, but nothing currently forces
  that to be true before a merge is allowed.
- No required PR review count, no required approving review, no
  dismiss-stale-reviews setting -- because no protection rule exists to
  carry any of those settings.

## Rulesets

`repos/kimeisele/agent-village/rulesets` → `[]`. No repository rulesets
(the newer, more flexible alternative/supplement to classic branch
protection) exist either.

## Required status checks

None (see Branch protection above -- `required_status_checks.checks: []`,
`enforcement_level: "off"`).

## Force-push / deletion settings

Not restricted (no protection rule exists to restrict them). This is a
platform default, not a repo-specific opt-in.

## PR review requirements

None enforced by GitHub. Every PR in this project's history so far (#3
through #15) required an explicit review because Kim's own working
process demanded it ("kein Blind-Merge") -- not because the platform
would have refused an unreviewed merge.

## Merge methods

`repos/kimeisele/agent-village` →
`{"allow_squash_merge": true, "allow_merge_commit": true,
"allow_rebase_merge": true, "allow_auto_merge": false,
"delete_branch_on_merge": false}`.

All three merge strategies (squash/merge-commit/rebase) are available;
none is enforced as the only option. `allow_auto_merge: false` --
GitHub's "auto-merge when checks pass" feature is off. Every merge so
far in this project (PR #3–#15) has in fact used squash, by convention
(and Kim's explicit instruction for PR #15), not because merge-commit or
rebase are blocked.

## GitHub Actions permissions

`repos/kimeisele/agent-village/actions/permissions` →
`{"enabled": true, "allowed_actions": "all", "sha_pinning_required":
false}`. Actions are enabled repo-wide; any action from any source
(not restricted to verified/GitHub-authored actions) is permitted to
run. `sha_pinning_required: false` -- workflows in this repo are free to
reference actions by tag (`actions/checkout@v4`) rather than a pinned
commit SHA, and nothing at the platform level enforces otherwise (every
workflow in this repo currently does use tag references, e.g.
`actions/checkout@v4`, `actions/setup-python@v5`, `actions/
upload-artifact@v4` -- not pinned SHAs).

## Workflow permissions (default `GITHUB_TOKEN` scope)

`repos/kimeisele/agent-village/actions/permissions/workflow` →
`{"default_workflow_permissions": "read", "can_approve_pull_request_
reviews": false}`. The repo-wide default for any workflow that doesn't
declare its own `permissions:` block is **read-only** -- a genuinely
safe default. Every security-critical workflow added in this project
(`worker-proof-01.yml`, `operator-execute-01.yml`) additionally declares
its own explicit `permissions: contents: read` regardless, so this isn't
being relied on silently -- it's a second, independent confirmation that
those workflows can't write, not the only one.

## Secrets and variables (names only, no values read or requested)

Repository secrets (`actions/secrets`, names + `updated_at` only, values
never retrievable via the API and never requested here):
- `DEEPSEEK_API_KEY` (updated 2026-07-19T11:02:13Z)
- `MOLTBOOK_API_KEY` (updated 2026-07-18T19:04:30Z)
- `NODE_PRIVATE_KEY` (updated 2026-07-18T19:04:40Z)

Repository variables (`actions/variables`, non-secret by definition):
- `MB_REG_POST` (updated 2026-07-18T20:09:45Z)

No `FEDERATION_PAT` or any other broader-scoped credential exists
(consistent with `docs/BEFUND.md`'s earlier token-scope cleanup, §20).

## Environments

`repos/kimeisele/agent-village/environments` → `{"total_count": 0,
"environments": []}`. No GitHub Environments configured -- no
environment-scoped secrets, no environment-level required reviewers, no
deployment branch restrictions. All three secrets above are plain
repository secrets, available to any workflow run in this repo (subject
to that workflow's own `permissions:`/trigger restrictions, which for
the two DeepSeek-calling workflows are `workflow_dispatch`-only +
`contents: read`, as documented in their own files and in `docs/
research/INTERNAL_WORKER_PROOF_01.md`/`AGENT_LOOP_WORKER_02.md`).

## Who can bypass protections

Moot today -- there is nothing to bypass (`main` is unprotected, see
above). `repos/kimeisele/agent-village/collaborators` lists exactly one
collaborator: `kimeisele`, with `admin: true, maintain: true, push:
true, triage: true, pull: true` -- full repository admin. No other
collaborator, no team, no bot account has any access. If branch
protection is ever enabled, `kimeisele` (as the sole admin) would be the
only account whose bypass permissions would need to be explicitly
considered.

## Summary — the actual current posture

The repository's security today rests entirely on: (1) working
discipline (every slice in this project has gone through a branch + PR
+ independent review, by convention, not platform enforcement), (2) the
individual workflows' own explicit `permissions:` blocks and
`workflow_dispatch`-only triggers for anything DeepSeek-capable, and (3)
a safe repo-wide default (`GITHUB_TOKEN` read-only unless a workflow
opts in to more). None of that is backed by a platform-level guarantee
on `main` itself -- no required reviews, no required status checks, no
protection against a direct push or force-push, by anyone with write
access (currently just `kimeisele`). This is a real, current gap between
"how work has actually been done" and "what GitHub itself would prevent
if the discipline lapsed once."

**Not acted on in this PR**, per Kim's explicit instruction: no ruleset
activated, no branch protection enabled. That decision -- and getting it
right (especially not accidentally locking out the sole admin account,
or a future automation identity, from ever merging) -- is deliberately
left for Kim to make explicitly, informed by this document, not
defaulted to here.
