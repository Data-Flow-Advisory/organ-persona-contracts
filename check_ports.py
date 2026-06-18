#!/usr/bin/env python3
"""Port-declaration checker for the Persona Contracts organ.

Asserts the connection-standard contract between ``ports.json`` and the
organ's ``decide(state, context)`` implementation:

  1. ``ports.json`` parses and has the required shape
     (``inputs: [{name, type, required}]``, ``outputs: [{name, type}]``).
  2. Every ``type`` referenced in ``ports.json`` exists in the vocabulary
     declared by ``types.json``.
  3. ``decide`` reads EXACTLY the declared input names — the set of
     ``state.get("<name>")`` literals in ``organ.py`` equals the set of
     declared ``inputs[].name``.
  4. ``decide`` writes EXACTLY the declared output names — the set of keys
     of every dict literal that is the value of an ``"output"`` key in
     ``organ.py`` equals the set of declared ``outputs[].name``.

Exits non-zero with a clear message on any violation, so the conformance
workflow goes RED if ports.json ever drifts from the implementation.

Usage (in CI):
    python check_ports.py
"""
from __future__ import annotations

import ast
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PORTS = os.path.join(_HERE, "ports.json")
_TYPES = os.path.join(_HERE, "types.json")
_ORGAN = os.path.join(_HERE, "organ.py")


def _fail(msg: str) -> int:
    print(f"ports check FAILED: {msg}")
    return 1


def _load_vocabulary() -> set[str]:
    with open(_TYPES) as fh:
        doc = json.load(fh)
    types = doc.get("types")
    if isinstance(types, dict):
        return set(types.keys())
    if isinstance(types, list):
        # tolerate a list of {"name": ...} or bare strings
        out: set[str] = set()
        for t in types:
            if isinstance(t, str):
                out.add(t)
            elif isinstance(t, dict) and isinstance(t.get("name"), str):
                out.add(t["name"])
        return out
    raise ValueError("types.json must carry a 'types' object or list")


def _extract_state_reads(tree: ast.AST) -> set[str]:
    """Every literal key passed to ``state.get("<name>")`` in the source."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Name)
            and func.value.id == "state"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _extract_output_keys(tree: ast.AST) -> set[str]:
    """Keys of every dict literal that is the value of an ``"output"`` key.

    The organ returns ``{"output": {"valid": ..., "error": ...}, ...}`` from
    each path; this collects the keys of those inner ``output`` dicts.
    """
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if (
                isinstance(k, ast.Constant)
                and k.value == "output"
                and isinstance(v, ast.Dict)
            ):
                for inner_k in v.keys:
                    if isinstance(inner_k, ast.Constant) and isinstance(
                        inner_k.value, str
                    ):
                        keys.add(inner_k.value)
    return keys


def main() -> int:
    # 1. ports.json parses and has the required shape.
    try:
        with open(_PORTS) as fh:
            ports = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"ports.json does not parse: {exc}")

    if not isinstance(ports, dict):
        return _fail("ports.json must be a JSON object")

    inputs = ports.get("inputs")
    outputs = ports.get("outputs")
    if not isinstance(inputs, list) or not isinstance(outputs, list):
        return _fail("ports.json must carry 'inputs' and 'outputs' arrays")

    declared_inputs: set[str] = set()
    for i, port in enumerate(inputs):
        if not isinstance(port, dict):
            return _fail(f"inputs[{i}] is not an object")
        name, typ, req = port.get("name"), port.get("type"), port.get("required")
        if not isinstance(name, str) or not name:
            return _fail(f"inputs[{i}] missing a string 'name'")
        if not isinstance(typ, str) or not typ:
            return _fail(f"input '{name}' missing a string 'type'")
        if not isinstance(req, bool):
            return _fail(f"input '{name}' missing a boolean 'required'")
        declared_inputs.add(name)

    declared_outputs: set[str] = set()
    for i, port in enumerate(outputs):
        if not isinstance(port, dict):
            return _fail(f"outputs[{i}] is not an object")
        name, typ = port.get("name"), port.get("type")
        if not isinstance(name, str) or not name:
            return _fail(f"outputs[{i}] missing a string 'name'")
        if not isinstance(typ, str) or not typ:
            return _fail(f"output '{name}' missing a string 'type'")
        declared_outputs.add(name)

    # 2. Every referenced type exists in the vocabulary.
    try:
        vocab = _load_vocabulary()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _fail(f"types.json does not load: {exc}")

    for port in inputs + outputs:
        typ = port["type"]
        if typ not in vocab:
            return _fail(
                f"port '{port['name']}' uses type '{typ}' not in types.json "
                f"vocabulary {sorted(vocab)}"
            )

    # 3 + 4. decide reads/writes exactly the declared names.
    with open(_ORGAN) as fh:
        tree = ast.parse(fh.read())

    reads = _extract_state_reads(tree)
    if reads != declared_inputs:
        missing = reads - declared_inputs
        extra = declared_inputs - reads
        return _fail(
            "declared inputs do not match state.get() reads in organ.py — "
            f"read-but-undeclared={sorted(missing)}, "
            f"declared-but-unread={sorted(extra)}"
        )

    writes = _extract_output_keys(tree)
    if writes != declared_outputs:
        missing = writes - declared_outputs
        extra = declared_outputs - writes
        return _fail(
            "declared outputs do not match keys written under 'output' in "
            f"organ.py — written-but-undeclared={sorted(missing)}, "
            f"declared-but-unwritten={sorted(extra)}"
        )

    print(
        "ports check OK: "
        f"{len(declared_inputs)} input(s) {sorted(declared_inputs)} and "
        f"{len(declared_outputs)} output(s) {sorted(declared_outputs)} "
        "match organ.py; all types in vocabulary"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
