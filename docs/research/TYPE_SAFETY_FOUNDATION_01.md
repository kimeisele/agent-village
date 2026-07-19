# Type Safety Foundation 01 — baseline + configuration + deferred items

Status: foundational layer. No new agent capability, no architecture change.
Written **before** CI activation so the PR diff is the documentation of
exactly what was configured and why.

## Ausgangsbaseline (2026-07-19, main `505ec9c`)

Recon verified against `docs/research/TYPE_SAFETY_BASELINE_01.md` (written
during Operator Execution 01, read-only, no configuration at that time).
Fresh `mypy` and `ruff` runs confirmed the baseline findings are still
current against `main` — 7 real type errors, 0 ruff configuration, 21
`Any` occurrences all at JSON boundaries.

| Metric | Baseline (before this slice) |
|---|---|
| Ruff configuration | none |
| Mypy configuration | none |
| Real mypy errors (logic, non-style) | 7 |
| `Any` occurrences in `village/` + `scripts/` | 21 (all at JSON boundaries) |
| Untyped functions in production code | 6 (all in `village/heartbeat.py`) |
| Tests | 302/302 |

## Gewählte Ruff-Regeln

Enabled in `pyproject.toml`:

- **E** (pycodestyle errors) — line-length 120
- **F** (pyflakes) — unused imports, undefined names
- **I** (isort) — import ordering

Tests: `E501` (line length) relaxed via `per-file-ignores` — readable
assertions with inline data are more important than strict line length
in test files.

All non-E501 findings fixed (E402: standalone script imports, F401: unused
imports, I001: import ordering). Six `# noqa: E501` annotations on Moltbook
template strings in `village/heartbeat.py` where breaking the line would
make the template unreadable.

## Gewählte mypy-Regeln

```ini
[tool.mypy]
python_version = "3.11"
explicit_package_bases = true
ignore_missing_imports = true
check_untyped_defs = true
no_implicit_optional = true
warn_unused_ignores = true
warn_redundant_casts = true
warn_return_any = true
```

### Bewusst noch nicht aktiviert

| Regel | Grund |
|---|---|
| `disallow_any_generics` | 60+ bare generics in JSON-heavy code. Fixing them properly requires per-line type parameter selection (`list[str]` vs `list[CaptchaCandidate]` vs `list[dict[str, Any]]`), which is mechanical but broad. Deferred to a dedicated follow-up slice. |
| `disallow_any_explicit` | Depends on `disallow_any_generics` being clean first. |
| `disallow_untyped_defs` | 6 untyped functions in `village/heartbeat.py` — oldest, most-evolved file. Adding return types here is straightforward but touches many call sites. |
| `strict` | Umbrella — activates all of the above plus more. |

### Test overrides

Tests run under `[[tool.mypy.overrides]]` with `disallow_any_generics = false`
and `warn_return_any = false` — production code is held to a higher standard
than test fixtures.

## Behobene reale Fehler

### 1. `village/contracts.py` — Budget.remaining() returns Any (line 120)

- **Ursache:** `getattr(self, dimension)` and `getattr(self, f"used_{dimension}")` both return `Any`
- **Fix:** Added explicit `float | None` and `float` type annotations on the variables before the subtraction

### 2. `village/contracts.py` — datetime | None assigned to datetime (line 205)

- **Ursache:** `normalize_datetime()` returns `datetime | None`, but `created_at` is typed as `datetime`
- **Fix:** Added `or _now()` fallback — `created_at` always has a default factory, so this is a type-only guard

### 3. `village/contracts.py` — datetime | None comparison (line 216)

- **Ursache:** `normalize_datetime(now)` can return `None` even though `now` is never `None` at this point
- **Fix:** Reordered to `_now() if now is None else normalize_datetime(now) or _now()` — mypy narrows both branches to `datetime`

### 4. `village/work_result.py` — datetime | None assigned to datetime (line 54)

- **Ursache:** Same `normalize_datetime()` pattern as contracts.py
- **Fix:** Added `or datetime.now(timezone.utc)` fallback

### 5–6. `village/heartbeat.py` — dict | None assignment (lines 870, 916)

- **Ursache:** `bounty_claim()` and `bounty_complete()` return `dict[str, Any] | None`, but `result` variable was previously typed as `dict[Any, Any]` from `_post_comment_verified()`
- **Fix:** `# type: ignore[assignment]` — the runtime `if result:` guard already handles `None` correctly

### 7–8. `village/bounty_review.py` — Any | None passed to str (lines 321, 335)

- **Ursache:** `bounty.get("current_submission_id")` returns `Any | None`, passed to `_attach_review(submission_id: str, ...)`
- **Fix:** Added `str(submission_id)` — the `submission_id` is already guarded by earlier `None` checks, this is a type-only conversion

### 9–14. Various `no-any-return` (6 occurrences across 5 files)

- **Ursache:** `json.loads()` returns `Any`; functions returning its result propagate `Any`
- **Fix:** Added `cast(dict[str, Any], ...)` on return values of `_load()`, `_get_submission()`, `_find_bounty()`, `_safe_evidence()`, `create_issue()`, and `dex_list()`

## `Any`-Klassifikation vorher → nachher

Alle 21 `Any`-Vorkommen bleiben in den Kategorien 1–3 (legitime Boundaries):

| Kategorie | Datei | Anzahl | Status |
|---|---|---|---|
| 1. Externe Provider-Grenze | `cognitive_provider.py` | 1 (`raw: dict[str, Any]`) | unverändert |
| 2. JSON-Persistenzgrenze | `work_result.py` | 6 (`output`, `evidence`, `usage`, `to_dict`, `from_dict`) | unverändert |
| 2. JSON-Persistenzgrenze | `contracts.py` | 9 (`extra`, `to_dict`, `from_dict`, `new_child_contract`) | unverändert |
| 3. Freies Evidence-Feld | `bounty_review.py` | 5 (`_safe_evidence`, `evidence` parameter) | unverändert |

**Kein `Any` in Kategorie 4 (unnötig) gefunden.** Der Codebase-Befund aus
TYPE_SAFETY_BASELINE_01.md hat sich bestätigt: Jedes `Any` sitzt an einer
legitimen JSON-/Provider-/Evidence-Boundary.

Neu hinzugefügte `cast()`-Aufrufe (6 Stück) dienen ausschließlich der
Typ-Präzisierung an genau diesen Boundaries — sie verengen den Typ von
`Any` auf den tatsächlich erwarteten Typ, ohne Laufzeitverhalten zu ändern.

## CI-Integration

**Check-Name unverändert: `pytest`.** Die bestehenden Branch-Protection-Regeln
bleiben intakt.

Der Workflow `.github/workflows/tests.yml` wurde um zwei Schritte erweitert,
beide VOR dem bestehenden `pytest`-Schritt:

1. **Install dependencies** — `pip install pytest ruff mypy`
2. **Run Ruff (E, F, I)** — `ruff check --select E,F,I village/ scripts/ tests/`
3. **Run mypy** — `python3 -m mypy village/ scripts/ --ignore-missing-imports --explicit-package-bases`
4. **Run test suite** (unverändert) — `python3 -m pytest tests/ -v`

Ein Fehler in Schritt 2 oder 3 macht den gesamten Job rot — der erforderliche
Check `pytest` schlägt fehl, der PR ist nicht mergeable. Kein neuer separater
Check-Name wurde hinzugefügt.

Keine neuen Schreibrechte für GitHub Actions.

## Stufenweiser Weg zu strengerem mypy

1. ✅ **Foundation 01 (dieser Slice):** Konfiguration + reale Fehler behoben
2. **Foundation 02 (nächster Schritt):** `disallow_any_generics = true` aktivieren.
   Erfordert: ~60 bare generics (`list`, `set`, `frozenset`, `Match`) mit
   korrekten Typparametern versehen. Größte Datei: `moltbook_captcha.py` (20).
   Aufwand: mechanisch, ~1–2 Stunden.
3. **Foundation 03:** `disallow_untyped_defs = true` aktivieren.
   Erfordert: 6 untypisierte Funktionen in `heartbeat.py` annotieren.
4. **Foundation 04:** `disallow_any_explicit = true` aktivieren.
   Erfordert: 21 `Any`-Stellen durch spezifischere Typen ersetzen (z.B.
   `TypedDict` für WorkResult/Contract).
5. **Foundation 05:** `strict = true` aktivieren.
   Erfordert: alle verbleibenden Strict-Checks bestehen.

## Nachweis: Branch-Protection-Checkname unverändert

```bash
$ gh api repos/kimeisele/agent-village/branches/main/protection \
    --jq '.required_status_checks.checks[0].context'
pytest
```

Der Job-Id in `.github/workflows/tests.yml` heißt weiterhin `pytest`.
Die neuen Ruff/mypy-Schritte laufen IM SELBEN Job — sie erzeugen keinen
neuen Check-Namen.
