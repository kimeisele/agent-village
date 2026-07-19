# Type Safety Foundation 01 — final configuration and boundary model

Status: active. This is the single authoritative specification for the
current type-safety configuration. Replaces the earlier draft written
during the initial (rejected) PR approach.

## Rejected first attempt

The initial PR version used `cast(dict[str, Any], json.loads(...))` at
persistence boundaries, deferred `disallow_any_generics`, and suppressed
variable-reuse issues with `# type: ignore[assignment]`. All of these
were rejected during review and have been replaced.

## Ausgangsbaseline (2026-07-19, main `505ec9c`)

See `docs/research/TYPE_SAFETY_BASELINE_01.md` (written during Operator
Execution 01) — 7 real mypy errors, 0 type-checker configuration,
21 `Any` occurrences at JSON boundaries.

## Finale Ruff-Regeln

```toml
[tool.ruff]
target-version = "py311"
line-length = 120

[tool.ruff.lint]
select = ["E", "F", "I", "W"]
ignore = ["E501"]
```

E501 is delegated to `ruff format`. CI runs both `ruff check` and
`ruff format --check`.

### Verbleibende Ruff-Ausnahmen

- 3× `# noqa: E402` in `scripts/operator_execute.py` — standalone script
  bootstrap, must add repo root to sys.path before importing `village.*`.

## Finale mypy-Regeln

```toml
[tool.mypy]
python_version = "3.11"
explicit_package_bases = true
disallow_any_generics = true
disallow_untyped_defs = true
check_untyped_defs = true
no_implicit_optional = true
warn_unused_ignores = true
warn_redundant_casts = true
warn_return_any = true
strict_equality = true
```

**Nicht benötigt:** `ignore_missing_imports`. `cryptography` ist PEP 561
und installiert. `village/nadi_bridge.py` hat keine mypy-Fehler.

**Tests:** Nicht im mypy-Scope (CI läuft `mypy village scripts`).

## JSON-Boundary-Modell

### `village/_types.py`

```python
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]
```

Python 3.11-kompatibel via `TypeAlias` statt `type`-Statement (3.12+).

### `load_json_object(data: str | bytes) -> JsonObject`

Rekursive Validierung:
- Top-Level ist ein `dict`
- Alle Keys sind `str`
- Alle Werte sind gültige `JsonValue` (rekursiv)
- `NaN`, `Infinity`, `-Infinity` werden abgelehnt (nicht valide JSON
  per RFC 8259)

### `is_json_value(obj: object) -> TypeGuard[JsonValue]`

Rekursiver TypeGuard — prüft alle JSON-Kompatibilitätsbedingungen
ohne I/O.

### Persistenzgrenze: `_load()` / `_save()`

```python
def _load(p: Path) -> JsonObject:
    if not p.exists():
        return {}
    return load_json_object(p.read_text())

def _save(p: Path, data: JsonObject) -> None:
    ...
```

`_load()` ist DIE zentrale validierte Persistenzgrenze. Kein `cast()`.

### API-Grenze: `_api()` / `_gh()` / `_mb()`

```python
def _api(...) -> JsonValue | None:
def _gh(...) -> JsonValue | None:
def _mb(...) -> JsonValue | None:
```

Call-Sites, die `dict`-Strukturen erwarten, verwenden `isinstance(resp, dict)`-Guards.

### Call-Site-Pattern für `JsonObject`-Felder

```python
agents_raw = dex.get("agents", [])
if isinstance(agents_raw, list):
    for agent in agents_raw:
        if isinstance(agent, dict):
            ...
```

## Behobene Typfehler

1. `contracts.py`: `Budget.remaining()` — `getattr` returns `Any` → typed variables
2. `contracts.py`: `__post_init__` — `normalize_datetime` returns `datetime | None` → `or _now()`
3. `contracts.py`: `is_past_deadline` — same pattern
4. `work_result.py`: `__post_init__` — same pattern
5. `heartbeat.py`: `_record_contribution` — `artifact_refs` typed `list[str]` not `list[dict[str, Any]]`
6. `bounty_review.py`: `submission_id` — isinstance guard replaces `str()` conversion
7. `bounty_review.py`: `_find_bounty`/`_get_submission` — isinstance guards replace `cast()`
8. Diverse `no-any-return` (6×) — durch Boundary-Typen behoben

## Verbleibende `Any`-Stellen

| Datei | Stelle | Kategorie |
|---|---|---|
| `cognitive_provider.py` | `CognitiveResponse.raw: dict[str, Any]` | Provider-Payload |
| `work_result.py` | `output`, `evidence`, `usage`, `to_dict`, `from_dict` | JSON-Persistenz freie Form |
| `contracts.py` | `extra: dict[str, Any]`, `to_dict`, `from_dict` | Schema-toleranter Bucket |
| `bounty_review.py` | `_safe_evidence()`, `evidence`-Parameter | Freies Evidence-Dict |
| `moltbook_captcha.py` | `mb_call: Any` | Externer Callable-Typ |
| `heartbeat.py` | `_api()` return | `JsonValue` — dies IST die typisierte Boundary |

Alle verbleibenden `Any` sitzen an legitimen externen Grenzen (Kategorien 1–3).

## Stufenweiser Weg zu `strict = true`

1. ✅ **Foundation 01 (dieser Slice):** Konfiguration + Boundaries + reale Fehler
2. **Foundation 02:** `disallow_any_explicit = true` — 21 `Any`-Stellen durch spezifischere Typen ersetzen
3. **Foundation 03:** `strict = true` — alle verbleibenden Strict-Checks

## CI-Integration

Job-Name `pytest` unverändert. Schritte:
1. `pip install -r requirements-dev.txt`
2. `ruff check village/ scripts/ tests/`
3. `ruff format --check village/ scripts/ tests/`
4. `python3 -m mypy village scripts`
5. `python3 -m pytest tests/ -v`

## Dev-Dependencies

`requirements-dev.txt`:
- `pytest==8.0.0` — mit 327 Tests verifiziert, Python 3.11 kompatibel
- `ruff==0.8.1` — lokal installiert, formatiert korrekt
- `mypy==1.18.2` — lokal installiert, alle 8 Regeln sauber
