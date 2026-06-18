"""Tests for the ports.json declaration and its checker (check_ports.py).

These run under the same ``pytest -v`` invocation the conformance workflow
uses, and additionally negative-test the checker so a future drift between
ports.json and organ.py is provably caught.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys

import check_ports

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name: str) -> dict:
    with open(os.path.join(_HERE, name)) as fh:
        return json.load(fh)


def test_ports_json_parses_and_shape():
    ports = _load("ports.json")
    assert isinstance(ports["inputs"], list)
    assert isinstance(ports["outputs"], list)
    for port in ports["inputs"]:
        assert isinstance(port["name"], str) and port["name"]
        assert isinstance(port["type"], str) and port["type"]
        assert isinstance(port["required"], bool)
    for port in ports["outputs"]:
        assert isinstance(port["name"], str) and port["name"]
        assert isinstance(port["type"], str) and port["type"]


def test_types_json_parses_and_has_vocabulary():
    vocab = check_ports._load_vocabulary()
    assert {"string", "integer", "number", "boolean", "object", "array"} <= vocab


def test_every_port_type_in_vocabulary():
    ports = _load("ports.json")
    vocab = check_ports._load_vocabulary()
    for port in ports["inputs"] + ports["outputs"]:
        assert port["type"] in vocab, port


def test_declared_inputs_match_state_reads():
    declared = {p["name"] for p in _load("ports.json")["inputs"]}
    with open(os.path.join(_HERE, "organ.py")) as fh:
        tree = ast.parse(fh.read())
    assert check_ports._extract_state_reads(tree) == declared


def test_declared_outputs_match_output_keys():
    declared = {p["name"] for p in _load("ports.json")["outputs"]}
    with open(os.path.join(_HERE, "organ.py")) as fh:
        tree = ast.parse(fh.read())
    assert check_ports._extract_output_keys(tree) == declared


def test_check_ports_passes_on_repo_as_is():
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "check_ports.py")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_check_ports_fails_on_undeclared_input(tmp_path, monkeypatch):
    # A ports.json missing one of the inputs the organ actually reads must FAIL.
    ports = _load("ports.json")
    ports["inputs"] = [p for p in ports["inputs"] if p["name"] != "contract"]
    bad = tmp_path / "ports.json"
    bad.write_text(json.dumps(ports))
    monkeypatch.setattr(check_ports, "_PORTS", str(bad))
    assert check_ports.main() == 1


def test_check_ports_fails_on_unknown_type(tmp_path, monkeypatch):
    ports = _load("ports.json")
    ports["inputs"][0]["type"] = "not_a_real_type"
    bad = tmp_path / "ports.json"
    bad.write_text(json.dumps(ports))
    monkeypatch.setattr(check_ports, "_PORTS", str(bad))
    assert check_ports.main() == 1


def test_check_ports_fails_on_extra_undeclared_output(tmp_path, monkeypatch):
    ports = _load("ports.json")
    ports["outputs"].append({"name": "ghost", "type": "string"})
    bad = tmp_path / "ports.json"
    bad.write_text(json.dumps(ports))
    monkeypatch.setattr(check_ports, "_PORTS", str(bad))
    assert check_ports.main() == 1
