#!/usr/bin/env python3
"""
Persona Contracts Organ — extracted decision logic from discovery-engine.

A pure decider that gates persona behaviour at the perimeter. It folds the
three perimeter validators that lived in
``app/services/persona_contracts.py`` into one ``decide(state, context)``
entry point, dispatched by the ``check`` discriminator:

  * ``check == "result"``    — validate a ``/complete.result`` payload
                               against a registered output schema.
  * ``check == "directive"`` — reject inbound directives that conflict
                               with the persona's declared ``non_goals``.
  * ``check == "limits"``    — check observed usage against the persona's
                               budget / rate limits.

Contract:
  INPUT state: {
    "check": "result" | "directive" | "limits",

    # check == "result"
    "schema_id": str,
    "schema": {                          # the registered output schema,
      "required_fields": [str, ...],     # pre-resolved by the caller so the
      "field_types": {field: typename}   # organ stays DB/registry-free
    } | null,                            # null => unknown schema_id
    "result": <any>,                     # the payload to validate

    # check == "directive"
    "contract": {                        # persona's contract block
      "non_goals": [str, ...],
      "limits": {...}
    },
    "directive_text": str,

    # check == "limits"
    "contract": {"limits": {...}},
    "daily_items_so_far": int,
    "daily_cost_usd_so_far": float,
    "concurrent_sessions": int
  }

  OUTPUT: {
    "output": {
      "valid": bool,                     # True => accept, False => reject
      "error": {...} | null              # error body mirroring the source
    },
    "rationale": "...",
    "self_metric": {
      "confidence": float,               # 1.0 when all facts present
      "decision_path": str               # which gate decided
    }
  }

``field_types`` carries type names as strings (JSON has no Python types):
``"str" | "int" | "float" | "bool" | "dict" | "list"``. ``"int"`` also
accepts ``bool`` only when explicitly named (Python bools are ints); the
organ keeps the source semantics where ``isinstance(value, int)`` is used.

The organ is pure:
  - Takes all inputs via JSON
  - Makes no DB / network / registry calls (the caller resolves the schema
    and contract and passes them in)
  - Returns only computed advice
  - Never raises on bad input (fail-open: accept, with low confidence)
"""

from __future__ import annotations

import json
import os
import sys


# JSON type-name -> Python type, for the "result" schema field_types check.
_TYPE_MAP = {
    "str": str,
    "string": str,
    "int": int,
    "integer": int,
    "float": float,
    "number": (int, float),
    "bool": bool,
    "boolean": bool,
    "dict": dict,
    "object": dict,
    "list": list,
    "array": list,
}


def _type_label(typename: str) -> str:
    """Human-readable type label for an error reason, matching the source."""
    mapped = _TYPE_MAP.get(typename)
    if mapped is None:
        return str(typename)
    if isinstance(mapped, tuple):
        return typename
    return mapped.__name__


def _decide_result(state: dict) -> dict:
    """Validate a result payload against a registered output schema.

    Mirrors ``persona_contracts.validate_result``: unknown schema_id is a
    distinct (rejecting) outcome; otherwise check required fields and the
    types of present declared fields.
    """
    schema_id = state.get("schema_id", "")
    schema = state.get("schema")
    result = state.get("result")

    # Unknown schema — caller passes schema=null when the id isn't registered.
    if schema is None:
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "unknown_schema_id",
                    "schema_id": schema_id,
                    "missing": [],
                    "invalid": [],
                },
            },
            "rationale": (
                f"schema_id '{schema_id}' is not registered "
                f"(caller passed schema=null)"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "unknown_schema"},
        }

    required = list(schema.get("required_fields") or ())

    if not isinstance(result, dict):
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "schema_violation",
                    "schema_id": schema_id,
                    "missing": required,
                    "invalid": [
                        {"field": "<root>", "reason": "result must be a dict"},
                    ],
                },
            },
            "rationale": (
                f"result for schema '{schema_id}' must be a dict, "
                f"got {type(result).__name__}"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "root_not_dict"},
        }

    missing: list[str] = [f for f in required if f not in result]

    invalid: list[dict[str, str]] = []
    field_types = schema.get("field_types") or {}
    for field, typename in field_types.items():
        if field not in result:
            continue
        value = result[field]
        # Allow None where the field isn't required (source semantics).
        if value is None and field not in required:
            continue
        expected = _TYPE_MAP.get(typename)
        if expected is None:
            # Unknown type declaration — can't check, skip (don't reject).
            continue
        if not isinstance(value, expected):
            invalid.append({
                "field": field,
                "reason": f"must be {_type_label(typename)}",
            })

    if missing or invalid:
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "schema_violation",
                    "schema_id": schema_id,
                    "missing": missing,
                    "invalid": invalid,
                },
            },
            "rationale": (
                f"result violates schema '{schema_id}': "
                f"{len(missing)} missing field(s), {len(invalid)} type error(s)"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "schema_violation"},
        }

    return {
        "output": {"valid": True, "error": None},
        "rationale": (
            f"result satisfies schema '{schema_id}': all required fields "
            f"present and declared types valid"
        ),
        "self_metric": {"confidence": 1.0, "decision_path": "schema_ok"},
    }


def _decide_directive(state: dict) -> dict:
    """Reject directives that conflict with the persona's non_goals.

    Mirrors ``persona_contracts.validate_directive``: conservative
    case-insensitive substring match. Empty directive or absent non_goals
    pass through (back-compat).
    """
    directive_text = state.get("directive_text")
    contract = state.get("contract") or {}
    non_goals = contract.get("non_goals") or []

    if not isinstance(directive_text, str) or not directive_text.strip():
        return {
            "output": {"valid": True, "error": None},
            "rationale": (
                "directive is empty/non-string; content validator passes "
                "(empty-directive is a separate bug class for the caller)"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "empty_directive"},
        }

    if not non_goals:
        return {
            "output": {"valid": True, "error": None},
            "rationale": (
                "persona declares no non_goals (or no contract); directive "
                "accepted (back-compat)"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "no_non_goals"},
        }

    directive_lower = directive_text.lower()
    matched: list[str] = []
    for non_goal in non_goals:
        if not isinstance(non_goal, str) or not non_goal.strip():
            continue
        if non_goal.strip().lower() in directive_lower:
            matched.append(non_goal)

    if matched:
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "directive_conflicts_with_non_goals",
                    "matched_non_goals": matched,
                    "hint": (
                        "Persona's contract explicitly lists these as "
                        "non_goals; either route the directive to a different "
                        "persona or remove the conflicting topic."
                    ),
                },
            },
            "rationale": (
                f"directive matches {len(matched)} declared non_goal(s): "
                f"{matched}"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "non_goal_match"},
        }

    return {
        "output": {"valid": True, "error": None},
        "rationale": (
            f"directive matches none of the {len(non_goals)} declared "
            f"non_goal(s); accepted"
        ),
        "self_metric": {"confidence": 1.0, "decision_path": "no_match"},
    }


def _decide_limits(state: dict) -> dict:
    """Check observed usage against the persona's contract limits.

    Mirrors ``persona_contracts.check_limits``: first limit hit (in order
    items -> cost -> concurrent) rejects. No limits => pass.
    """
    contract = state.get("contract") or {}
    limits = contract.get("limits") or {}

    daily_items = state.get("daily_items_so_far", 0)
    daily_cost = state.get("daily_cost_usd_so_far", 0.0)
    concurrent = state.get("concurrent_sessions", 0)

    if not limits:
        return {
            "output": {"valid": True, "error": None},
            "rationale": "persona declares no limits; usage accepted (back-compat)",
            "self_metric": {"confidence": 1.0, "decision_path": "no_limits"},
        }

    max_daily_items = limits.get("max_daily_items")
    if max_daily_items is not None and daily_items >= int(max_daily_items):
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "limit_exceeded",
                    "limit": "max_daily_items",
                    "current": daily_items,
                    "configured": int(max_daily_items),
                },
            },
            "rationale": (
                f"max_daily_items hit: {daily_items} >= {int(max_daily_items)}"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "max_daily_items"},
        }

    max_daily_cost = limits.get("max_daily_cost_usd")
    if max_daily_cost is not None and daily_cost >= float(max_daily_cost):
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "limit_exceeded",
                    "limit": "max_daily_cost_usd",
                    "current": round(daily_cost, 4),
                    "configured": float(max_daily_cost),
                },
            },
            "rationale": (
                f"max_daily_cost_usd hit: {round(daily_cost, 4)} >= "
                f"{float(max_daily_cost)}"
            ),
            "self_metric": {"confidence": 1.0, "decision_path": "max_daily_cost_usd"},
        }

    max_concurrent = limits.get("max_concurrent_sessions")
    if max_concurrent is not None and concurrent >= int(max_concurrent):
        return {
            "output": {
                "valid": False,
                "error": {
                    "error": "limit_exceeded",
                    "limit": "max_concurrent_sessions",
                    "current": concurrent,
                    "configured": int(max_concurrent),
                },
            },
            "rationale": (
                f"max_concurrent_sessions hit: {concurrent} >= "
                f"{int(max_concurrent)}"
            ),
            "self_metric": {
                "confidence": 1.0,
                "decision_path": "max_concurrent_sessions",
            },
        }

    return {
        "output": {"valid": True, "error": None},
        "rationale": "all configured limits within bounds; usage accepted",
        "self_metric": {"confidence": 1.0, "decision_path": "limits_ok"},
    }


_DISPATCH = {
    "result": _decide_result,
    "directive": _decide_directive,
    "limits": _decide_limits,
}


def decide(state: dict, context: dict | None = None) -> dict:
    """Decide whether a persona-contract perimeter check passes.

    Args:
        state: must carry a ``check`` discriminator selecting which
            validator to run, plus that validator's inputs (see module
            docstring).
        context: unused, present for orchestrator compatibility.

    Returns:
        {"output": {valid, error}, "rationale": "...", "self_metric": {...}}
    """
    context = context or {}

    try:
        check = state.get("check")
        handler = _DISPATCH.get(check)
        if handler is None:
            return {
                "output": {
                    "valid": True,
                    "error": {
                        "error": "unknown_check",
                        "check": check,
                        "supported": sorted(_DISPATCH.keys()),
                    },
                },
                "rationale": (
                    f"unknown check '{check}'; fail-open accept "
                    f"(supported: {sorted(_DISPATCH.keys())})"
                ),
                "self_metric": {"confidence": 0.0, "decision_path": "unknown_check"},
            }
        return handler(state)

    except Exception as e:  # pragma: no cover - defensive fail-open
        # Fail-open: on any error, accept. Blocking legitimate persona work
        # on a validator bug is worse than letting a payload through; the
        # low confidence flags the decision as untrusted for downstream
        # observers.
        return {
            "output": {"valid": True, "error": None},
            "rationale": f"decision logic error (fail-open accept): {e}",
            "self_metric": {"confidence": 0.0, "decision_path": "error_fallback"},
        }


def main() -> int:
    path = os.environ.get("ORGAN_INPUT")
    raw = open(path).read() if path else sys.stdin.read()
    try:
        payload = json.loads(raw)
        state = payload["state"]
    except Exception as e:
        print(json.dumps({"error": f"invalid input: {e}"}), file=sys.stderr)
        return 1
    print(json.dumps(decide(state, payload.get("context")), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
