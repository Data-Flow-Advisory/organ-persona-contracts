#!/usr/bin/env python3
"""
Connection-standard conformance for organ-persona-contracts.

Implements the port check the Connection Standard (orchestrator/CONNECTORS.md
§ "Conformance gains a port check") adds on top of the contract shape check.
It asserts, with no external services:

  1. ``ports.json`` parses and has the {inputs:[{name,type,required}],
     outputs:[{name,type}]} shape.
  2. Every declared ``type`` exists in the vendored ``types.json`` vocabulary.
  3. ``decide`` actually **reads** each declared input name and **writes**
     each declared output name — sampled against the organ's own committed
     samples, and proven by an instrumented state proxy that records every
     key the organ touches (stronger than a literal text match: it witnesses
     the real read, not just the name's presence in the file).

This organ is op-dispatched on ``state['check']`` (result | directive |
limits), so a given input is only read in its own mode. The read check is
therefore COLLECTIVE: each declared input name must be read by at least one
committed sample (not by every sample). Every input is ``required: false``
accordingly, and that is asserted.

Run standalone (``python3 ports_check.py``) — exits non-zero on any failure —
or via the pytest tests in ``test_organ.py`` which call ``check_ports()``.
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


class _TrackingState(dict):
    """A dict that records every key the organ reads.

    Captures ``state[k]``, ``state.get(k)`` and ``k in state`` so the read
    check witnesses an actual access by ``decide`` rather than trusting that
    the name merely appears in the sample JSON.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.touched: set = set()

    def __getitem__(self, key):
        self.touched.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.touched.add(key)
        return super().get(key, default)

    def __contains__(self, key):
        self.touched.add(key)
        return super().__contains__(key)


def _fail(msg: str):
    raise AssertionError(f"ports.json conformance: {msg}")


def _load_json(name: str):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        _fail(f"{name} not found beside organ.py")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        _fail(f"{name} does not parse as JSON: {e}")


def check_ports() -> dict:
    """Run every conformance assertion. Returns a small summary dict."""
    from organ import decide  # local import so import errors surface here

    ports = _load_json("ports.json")
    vocab = _load_json("types.json")

    # --- shape of ports.json -------------------------------------------------
    if not isinstance(ports, dict):
        _fail("ports.json must be a JSON object")
    for side in ("inputs", "outputs"):
        if side not in ports or not isinstance(ports[side], list):
            _fail(f"ports.json must carry a list under '{side}'")

    types = vocab.get("types")
    if not isinstance(types, dict):
        _fail("types.json must carry a 'types' object")

    input_names: list[str] = []
    output_names: list[str] = []

    for p in ports["inputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"each input port needs name+type: {p!r}")
        if "required" not in p:
            _fail(f"each input port needs an explicit 'required': {p!r}")
        if not isinstance(p["required"], bool):
            _fail(f"input port 'required' must be a bool: {p!r}")
        # Op-dispatched: no input is present in every mode, so none is required.
        if p["required"] is not False:
            _fail(
                f"input '{p['name']}' is required=True, but this organ is "
                "op-dispatched and fail-opens on a missing input; every input "
                "must be required=False"
            )
        if p["type"] not in types:
            _fail(
                f"input port '{p['name']}' type '{p['type']}' is not in the "
                "vocabulary (types.json)"
            )
        input_names.append(p["name"])

    for p in ports["outputs"]:
        if not isinstance(p, dict) or "name" not in p or "type" not in p:
            _fail(f"each output port needs name+type: {p!r}")
        if p["type"] not in types:
            _fail(
                f"output port '{p['name']}' type '{p['type']}' is not in the "
                "vocabulary (types.json)"
            )
        output_names.append(p["name"])

    if not input_names:
        _fail("expected at least one input port")
    if not output_names:
        _fail("expected at least one output port")

    # --- read/write check against the committed samples ----------------------
    sdir = os.path.join(HERE, "samples")
    sample_files = [
        f for f in sorted(os.listdir(sdir)) if f.endswith(".json")
    ]
    if not sample_files:
        _fail("expected committed samples to exercise the ports")

    read_seen: set = set()       # input names actually read by decide()
    written_seen: set = set()    # output names actually written by decide()
    present_in_sample: set = set()  # input names literally present in a sample

    for fname in sample_files:
        payload = json.loads(open(os.path.join(sdir, fname)).read())
        raw_state = payload.get("state", {})
        if not isinstance(raw_state, dict):
            _fail(f"{fname}: state must be a dict")
        for name in input_names:
            if name in raw_state:
                present_in_sample.add(name)

        tracked = _TrackingState(raw_state)
        out = decide(tracked, payload.get("context"))

        # Witness reads: which declared inputs did decide actually touch?
        for name in input_names:
            if name in tracked.touched:
                read_seen.add(name)

        # Witness writes: which declared outputs appear under output?
        body = out.get("output")
        if not isinstance(body, dict):
            _fail(f"{fname}: decide().output must be a dict")
        for name in output_names:
            if name in body:
                written_seen.add(name)

    missing_reads = [n for n in input_names if n not in read_seen]
    if missing_reads:
        _fail(
            "these declared input ports are never read by decide() across the "
            f"committed samples: {missing_reads}"
        )

    missing_present = [n for n in input_names if n not in present_in_sample]
    if missing_present:
        _fail(
            "these declared input ports never appear in any committed sample "
            f"state (add a sample that exercises them): {missing_present}"
        )

    missing_writes = [n for n in output_names if n not in written_seen]
    if missing_writes:
        _fail(
            "these declared output ports are never written under decide()."
            f"output across the samples: {missing_writes}"
        )

    # --- source guard: each input name is referenced in organ.py -------------
    src = open(os.path.join(HERE, "organ.py")).read()
    for name in input_names:
        if not re.search(rf"""["']{re.escape(name)}["']""", src):
            _fail(
                f"input port '{name}' is declared but never referenced as a "
                "string literal in organ.py"
            )

    return {
        "inputs": input_names,
        "outputs": output_names,
        "samples_checked": len(sample_files),
        "types_in_vocab": len(types),
    }


def main() -> int:
    summary = check_ports()
    print("ports.json conformance OK:")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
