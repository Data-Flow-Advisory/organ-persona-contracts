# Persona Contracts Organ

A pure decider that gates persona behaviour at the perimeter, extracted from
discovery-engine's `app/services/persona_contracts.py`.

It folds the three perimeter validators that lived in that module into a single
`decide(state, context)` entry point, dispatched by the `check` discriminator:

| `check`       | Source function       | Question answered                                         |
|---------------|-----------------------|-----------------------------------------------------------|
| `"result"`    | `validate_result`     | Does a `/complete.result` payload satisfy its output schema? |
| `"directive"` | `validate_directive`  | Does an inbound directive conflict with the persona's `non_goals`? |
| `"limits"`    | `check_limits`        | Is the persona within its budget / rate limits?           |

## Why an organ

The source module was already pure (no Flask, no SQLAlchemy — callers pass the
observed state in). This organ keeps that property and makes the boundary
explicit: the caller resolves the schema (from the contract registry) and the
persona's contract block, then asks the organ for a verdict. The organ never
touches a DB, registry, or network.

## Input contract

The organ reads a JSON object with a `state` field. `state.check` selects the
validator; the remaining fields are that validator's inputs.

### `check == "result"`

```json
{
  "state": {
    "check": "result",
    "schema_id": "persona_work.v1",
    "schema": {
      "required_fields": ["summary", "findings"],
      "field_types": {"summary": "str", "findings": "list"}
    },
    "result": {"summary": "done", "findings": []}
  }
}
```

- **schema_id** (str): the registered schema's id (echoed into error bodies).
- **schema** (dict | null): the resolved schema. Pass `null` when `schema_id`
  is not registered — the organ returns the distinct `unknown_schema_id` error.
  - `required_fields` (list[str]): fields that must be present.
  - `field_types` (dict, optional): `field -> type-name`. Type names are JSON
    strings: `str`, `int`, `float`/`number`, `bool`, `dict`/`object`,
    `list`/`array`. Present declared fields are type-checked; a `None` value on
    a non-required field is allowed; an unrecognised type name is skipped (not a
    violation).
- **result** (any): the payload to validate. A non-dict result is a violation.

### `check == "directive"`

```json
{
  "state": {
    "check": "directive",
    "contract": {"non_goals": ["pricing decisions"]},
    "directive_text": "Make pricing decisions for the deal."
  }
}
```

- **contract** (dict): the persona's contract block. `non_goals` (list[str]) is
  the only field read here.
- **directive_text** (str): the inbound directive. Match is conservative —
  case-insensitive substring of a non-goal in the directive. Empty/non-string
  directive, or no `non_goals`, passes (back-compat).

### `check == "limits"`

```json
{
  "state": {
    "check": "limits",
    "contract": {"limits": {"max_daily_items": 20, "max_daily_cost_usd": 5.0}},
    "daily_items_so_far": 20,
    "daily_cost_usd_so_far": 3.10,
    "concurrent_sessions": 1
  }
}
```

- **contract** (dict): persona contract; `limits` sub-dict is read.
  Supported limits: `max_daily_items`, `max_daily_cost_usd`,
  `max_concurrent_sessions`. Checked in that order; first hit rejects.
- **daily_items_so_far** / **daily_cost_usd_so_far** / **concurrent_sessions**:
  the observed usage, pre-computed by the caller.

## Output contract

```json
{
  "output": {
    "valid": true,
    "error": null
  },
  "rationale": "result satisfies schema 'persona_work.v1': ...",
  "self_metric": {
    "confidence": 1.0,
    "decision_path": "schema_ok"
  }
}
```

- **output.valid** (bool): `true` → accept, `false` → reject.
- **output.error** (dict | null): on reject, an error body that mirrors the
  source module's shapes:
  - result: `{"error": "schema_violation" | "unknown_schema_id", "schema_id", "missing", "invalid"}`
  - directive: `{"error": "directive_conflicts_with_non_goals", "matched_non_goals", "hint"}`
  - limits: `{"error": "limit_exceeded", "limit", "current", "configured"}`
- **rationale** (str): human-readable explanation, derivable from state alone.
- **self_metric**:
  - `confidence` (float): `1.0` on a normal decision; `0.0` on the fail-open
    path (unknown check / internal error) to flag the verdict as untrusted.
  - `decision_path` (str): which gate decided — `schema_ok`, `schema_violation`,
    `unknown_schema`, `root_not_dict`, `no_non_goals`, `empty_directive`,
    `non_goal_match`, `no_match`, `no_limits`, `limits_ok`, `max_daily_items`,
    `max_daily_cost_usd`, `max_concurrent_sessions`, `unknown_check`,
    `error_fallback`.

## Usage

### Run on a sample

```bash
ORGAN_INPUT=samples/result_valid.json python organ.py
```

### Run tests

```bash
pip install pytest
pytest -v
```

### Integration into discovery-engine

The organ is pure. The caller pre-resolves everything the source module already
received as arguments:

```python
import json, subprocess
from app.contracts.persona_outputs import RESULT_SCHEMAS

state = {
    "check": "result",
    "schema_id": schema_id,
    "schema": RESULT_SCHEMAS.get(schema_id),   # None => unknown_schema_id
    "result": complete_payload["result"],
}
proc = subprocess.run(
    ["python", "organ.py"],
    input=json.dumps({"state": state}),
    capture_output=True, text=True,
)
verdict = json.loads(proc.stdout)
if not verdict["output"]["valid"]:
    reject(verdict["output"]["error"])
```

`field_types` in `RESULT_SCHEMAS` holds Python types; convert them to the
JSON type-name form (`int -> "int"`, `str -> "str"`, ...) when building `schema`.

## Design principles

- **Pure**: all inputs via JSON; no DB / network / registry calls; deterministic.
- **Fail-open**: on an unknown `check` or an internal error the organ *accepts*
  with `confidence = 0.0`. Blocking legitimate persona work on a validator bug is
  worse than letting a payload through; the low confidence flags it for review.
- **Faithful**: error-body shapes, gate ordering, and back-compat passthroughs
  match `app/services/persona_contracts.py` exactly.
- **Stdlib-only**: no external dependencies; runs anywhere Python 3.7+ is present.

## Source

Extracted from `discovery-engine/app/services/persona_contracts.py`
(audit W1-1 perimeter validators). See `docs/architecture/persona-contracts.md`
in that repo for the schema design.
