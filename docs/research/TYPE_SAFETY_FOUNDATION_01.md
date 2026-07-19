# Type Safety Foundation 01 — Variante B (pragmatische kompatible Boundary)

Status: active. Dies ist die einzige autoritative Spezifikation.

## Review-Historie

Initialer Ansatz (deferred disallow_any_generics, cast()-basiert,
type: ignore[assignment]) → verworfen. Variante A (JsonObject durchgängig)
→ 230+ Call-Site-Fehler, unverhältnismäßig für diesen Slice. Final:
Variante B.

## Ausgangsbaseline

Siehe `TYPE_SAFETY_BASELINE_01.md` — 7 mypy-Fehler, 0 Konfiguration.

## Ruff (E, F, I, W, ignore=["E501"])

E501 an ruff format delegiert. CI: `ruff check` + `ruff format --check .`.

## Mypy (8 Regeln)

disallow_any_generics, disallow_untyped_defs, check_untyped_defs,
no_implicit_optional, warn_unused_ignores, warn_redundant_casts,
warn_return_any, strict_equality. Kein ignore_missing_imports.

## Boundary-Modell (Variante B)

### Persistenz: `_load()` / `_save()`

```python
def _load(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    return dict(load_json_object(p.read_text()))

def _save(p: Path, data: dict[str, Any]) -> None:
    ...
```

`load_json_object()` validiert Laufzeit-JSON-Kompatibilität (rekursiv,
inkl. NaN/Infinity). `dict(...)` verbreitert den statischen Typ bewusst
auf `dict[str, Any]` — Übergangslösung zur Erhaltung bestehender
dynamischer Call Sites. Kein `cast()`.

### API: `_api()` / `_gh()` / `_mb()`

```python
def _api(...) -> Any:
def _gh(...) -> Any:
def _mb(...) -> Any:
```

Bewusst untypisierte externe HTTP-Grenze. Call-Sites mit Objektannahme
verwenden `isinstance(resp, dict)`-Guards.

### Nicht Bestandteil von Foundation 01

Ein durchgängiges `JsonValue`-Modell (Variante A) bleibt konkrete
technische Schuld für einen Folge-Slice.

## Cast-Inventur

| Cast | Ersetzt durch |
|---|---|
| `cast(dict[str, Any], json.loads(...))` — `_load()` | `dict(load_json_object(...))` |
| `cast(list[dict[str, Any]], ...)` — `dex_list()` | `isinstance(agents_raw, list)` |
| `cast(dict[str, Any], b)` — `bounty_claim()` | `isinstance(b, dict)` + ValueError |
| `cast(dict[str, Any], _clean(...))` — `_safe_evidence()` | `isinstance(result, dict)` + ValueError |
| `cast(dict[str, Any], b)` — `_find_bounty()` (2×) | `isinstance(b, dict)` + ValueError |
| `cast(dict[str, Any] \| None, ...)` — `_get_submission()` | `isinstance(sub, dict)` |
| `cast(dict[str, Any], json.loads(...))` — `create_issue()` | `isinstance(resp_raw, dict)` |
| `cast(str, numbers[0])` — captcha | `isinstance(result, str)` + None |

**9/9 Casts entfernt.** 5 `assert isinstance()` durch ValueError/None ersetzt
(assert ist mit `python -O` deaktivierbar — kein Boundary-Schutz).

## CI (Job-Name `pytest` unverändert)

1. `pip install -r requirements-dev.txt`
2. `ruff check`
3. `ruff format --check .`
4. `python3 -m mypy village scripts`
5. `python3 -m pytest tests/ -v`

## Dev-Dependencies

requirements-dev.txt: pytest==8.0.0, ruff==0.8.1, mypy==1.18.2

## Tests: 327/327 (25 neu in test_type_safety.py)

## Offen: disallow_any_explicit, strict = true, durchgängiges JsonValue
