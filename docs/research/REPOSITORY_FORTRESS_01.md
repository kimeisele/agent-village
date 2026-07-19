# Repository Fortress 01 — protecting `main` without locking out the sole maintainer

Status: protects the repository platform itself, not the agent. No new
agent capability, no axiom, no automatic review system, no type-safety
rework. Written and committed **before** any protection is activated
(this file documents the plan; activation is a separate, subsequent API
call, verified against this plan afterwards in the same PR/report).

## Recon (read-only, fresh via `gh api`, 2026-07-19)

| Fact | Value |
|---|---|
| `main` SHA at recon time | `637c49475a8dc78996a5e721ac6568c9b477a6dd` |
| Branch protection on `main` | none (`404 Branch not protected`) |
| Repository rulesets | none (`[]`) |
| Owner / repo type | `kimeisele` (User, personal account), `public` |
| `allow_squash_merge` / `allow_merge_commit` / `allow_rebase_merge` | all `true` |
| `allow_auto_merge` | `false` |
| `delete_branch_on_merge` | `false` |
| Actions: `enabled` / `allowed_actions` / `sha_pinning_required` | `true` / `"all"` / `false` |
| Actions default workflow permissions | `default_workflow_permissions: "read"`, `can_approve_pull_request_reviews: false` |
| Collaborators | sole collaborator `kimeisele`, `admin: true, maintain: true, push: true, triage: true, pull: true` |
| Repo secrets (names only) | `DEEPSEEK_API_KEY`, `MOLTBOOK_API_KEY`, `NODE_PRIVATE_KEY` |
| Active workflows | `Node Heartbeat`, `Operator Execute 01 (DeepSeek, manual only)`, `Tests`, `Agent Village Heartbeat`, `Worker Proof 01 (DeepSeek, manual only)` |

**Verified CI check name (not guessed):** queried
`GET /repos/kimeisele/agent-village/commits/{main_sha}/check-runs`
against the current `main` HEAD. Result: exactly one check run, app
`github-actions`, `conclusion: "success"`, **`name: "pytest"`**. This
comes directly from `.github/workflows/tests.yml`'s job id `pytest`
(the job has no `name:` override, so GitHub uses the job id as the
check-run/context name). The legacy commit-status endpoint
(`/commits/{sha}/status`) returns an empty `statuses` array — this repo
uses GitHub Actions check runs, not legacy statuses, so the required
status check must be configured by **check-run name `pytest`**, not a
status context.

## Chosen method: classic branch protection, not a ruleset

Both are technically available here (`visibility: public` grants free
classic branch protection *and* free repository rulesets on a personal
account — GitHub does not gate either behind a paid plan for public
repos). Classic protection is chosen because:

- It is the smaller, more auditable surface for a single-maintainer
  repo: one `PUT` to one well-documented endpoint
  (`/repos/{owner}/{repo}/branches/main/protection`), one corresponding
  `GET` to verify, one `DELETE` to fully roll back.
- Rulesets are the more powerful/composable mechanism (multiple rulesets
  can target the same branch, bypass lists, layered enforcement) — that
  power is exactly what is *not* needed for "one branch, one maintainer,
  one required check." Introducing a ruleset here would add a second,
  more complex settings surface for no behavioral gain over classic
  protection, and Kim's scope explicitly excludes adding new bypass
  actors/mechanisms.
- classic protection's `enforce_admins` flag maps directly and legibly
  onto the exact requirement in this slice (admin blocked from *direct
  pushes*, not blocked from *merging via PR* or *changing protection*)
  — see the Admin/Lockout section below.

## Exact rules to activate

`PUT /repos/kimeisele/agent-village/branches/main/protection` with:

```json
{
  "required_status_checks": {
    "strict": true,
    "checks": [{"context": "pytest"}]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": 0,
    "require_code_owner_reviews": false,
    "dismiss_stale_reviews": false
  },
  "restrictions": null,
  "required_conversation_resolution": true,
  "allow_force_pushes": false,
  "allow_deletions": false
}
```

Mapping to Kim's 10 requested rules:

1. **Changes only via PR** → `required_pull_request_reviews` present at
   all (its mere presence requires a PR before merge to `main`) +
   `enforce_admins: true` (so this also applies to the sole admin, not
   just hypothetical future collaborators).
2. **No required foreign approval, author can merge own PR** →
   `required_approving_review_count: 0`. A PR is still mandatory (rule
   1), but zero approvals are required to merge it — `kimeisele` can
   merge their own PR the moment required checks are green.
3. **Existing pytest check mandatory** → `required_status_checks.checks:
   [{"context": "pytest"}]`, using the verified name above.
4. **Branch must be up to date before merge (if it works cleanly with
   the existing check)** → `required_status_checks.strict: true`. This
   repo has exactly one linear check (`pytest`) with no matrix/fan-out,
   so strict mode works cleanly: GitHub re-requires the check to have
   run against the PR's merge with the current `main` tip, no
   stale-branch merges.
5. **Open review conversations must be resolved** →
   `required_conversation_resolution: true`.
6. **Force push disabled** → `allow_force_pushes: false`.
7. **Branch deletion disabled** → `allow_deletions: false`.
8. **Direct normal pushes to `main` blocked** → the combination of
   "PR required" (rule 1) and `enforce_admins: true` — without
   `enforce_admins`, GitHub's classic protection exempts admins from the
   "require a PR" rule entirely, which is exactly the gap this slice
   exists to close (the direct docs push in
   `637c49475a8dc78996a5e721ac6568c9b477a6dd` was possible precisely
   because no protection existed yet).
9. **No unnecessary Actions write-permission grant** → not touched at
   all by this change; `default_workflow_permissions` stays `"read"`,
   verified unchanged in the post-activation read-back.
10. **No bot/app bypass actor added** → `restrictions: null` (no push
    restriction allowlist at all, so nothing to add an actor to); no
    GitHub App installed or granted bypass in this change.

## Expected behavior

**Normal PR (any future contributor, including `kimeisele`):** must
branch, commit, open a PR against `main`; cannot push directly to
`main`; PR is mergeable once `pytest` is green against an up-to-date
merge with `main` and all review conversations are resolved; zero
approvals required; merge via squash/merge-commit/rebase all remain
available (unchanged repo-level merge-method settings); branch deletion
after merge is a manual action (`delete_branch_on_merge` stays `false`,
unrelated to this change).

**Admin (`kimeisele`) specifically, because `enforce_admins: true`:**
cannot `git push origin main` directly — rejected by GitHub with a
protected-branch error, verified empirically below. **Can** still: open
PRs; merge their own PR through the normal merge API/UI once `pytest`
is green (merging a PR is a distinct, always-permitted operation from
pushing directly to the ref, not something `enforce_admins` restricts);
read, modify, or remove branch protection at any time via
`repos/.../branches/main/protection` (a repository-admin permission,
entirely orthogonal to `enforce_admins`, which only governs whether the
*ref-update rules* apply to admins — it does not touch who may *change
those rules*).

## Rollback

Full removal, single call, always available to `kimeisele` regardless
of the state of the rules above (see Admin/Lockout section):

```
gh api -X DELETE repos/kimeisele/agent-village/branches/main/protection
```

Partial rollback (e.g. drop `enforce_admins` only, to unblock a direct
emergency push without removing the rest) is a `PUT` re-sending the same
JSON body with `enforce_admins: false`.

## Admin/Lockout risk and why it does not apply here

GitHub's classic branch protection does **not** force an all-or-nothing
choice between "admin fully locked out" and "admin fully bypasses
everything" — `enforce_admins` and repository-admin permission are two
independent axes:

- `enforce_admins: true` only affects whether the *branch's ref-update
  rules* (PR-required, status-checks-required, no-force-push, etc.)
  apply to admins too. It has no effect on the `admin: true` repository
  permission itself.
- Modifying/removing branch protection is gated purely by repository
  admin permission (`collaborators` recon above: `kimeisele` has
  `admin: true`), not by whether the protection currently in force
  happens to `enforce_admins`.

So there is no forced choice to make here between full lockout and
unrestricted bypass — the safest practically usable configuration
(`enforce_admins: true`, everything else as specified) gives real
protection against accidental direct pushes **and** leaves a guaranteed,
always-available, single-API-call rollback. This is verified
empirically in the practical test below, not just asserted from docs.

## What this change deliberately does not touch

No secrets, no collaborators, no repository visibility, no Actions
permissions (`enabled`/`allowed_actions`/`default_workflow_permissions`
all re-read and confirmed unchanged after activation), no new GitHub
Apps/bots, no bypass allowlist, no other branch besides `main`.
