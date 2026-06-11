"""
Pytest test suite for the persona contracts organ.

Tests cover the three folded validators dispatched by ``state["check"]``:
  - result   : output-schema validation (required fields + types)
  - directive: non_goals conflict detection
  - limits   : budget / rate-limit checks

Plus dispatch, fail-open, and self_metric behaviour.
"""

import json
import os
import subprocess
import sys

import pytest

from organ import decide


# ---------------------------------------------------------------------------
# check == "result"
# ---------------------------------------------------------------------------

class TestResultSchema:
    def test_valid_minimal(self):
        state = {
            "check": "result",
            "schema_id": "summary.v1",
            "schema": {"required_fields": ["summary"]},
            "result": {"summary": "done"},
        }
        out = decide(state)
        assert out["output"]["valid"] is True
        assert out["output"]["error"] is None
        assert out["self_metric"]["decision_path"] == "schema_ok"
        assert out["self_metric"]["confidence"] == 1.0

    def test_missing_required_field(self):
        state = {
            "check": "result",
            "schema_id": "summary.v1",
            "schema": {"required_fields": ["summary", "findings"]},
            "result": {"summary": "done"},
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["error"] == "schema_violation"
        assert err["missing"] == ["findings"]
        assert err["invalid"] == []
        assert out["self_metric"]["decision_path"] == "schema_violation"

    def test_unknown_schema_id(self):
        state = {
            "check": "result",
            "schema_id": "nope.v9",
            "schema": None,
            "result": {"summary": "x"},
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["error"] == "unknown_schema_id"
        assert err["schema_id"] == "nope.v9"
        assert out["self_metric"]["decision_path"] == "unknown_schema"

    def test_result_not_dict(self):
        state = {
            "check": "result",
            "schema_id": "summary.v1",
            "schema": {"required_fields": ["summary"]},
            "result": "a string, not a dict",
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["error"] == "schema_violation"
        assert err["missing"] == ["summary"]
        assert err["invalid"][0]["field"] == "<root>"
        assert out["self_metric"]["decision_path"] == "root_not_dict"

    def test_result_none_not_dict(self):
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {"required_fields": []},
            "result": None,
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        assert out["output"]["error"]["invalid"][0]["reason"] == "result must be a dict"

    def test_type_check_str_pass(self):
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": ["summary"],
                "field_types": {"summary": "str"},
            },
            "result": {"summary": "ok"},
        }
        assert decide(state)["output"]["valid"] is True

    def test_type_check_str_fail(self):
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": ["summary"],
                "field_types": {"summary": "str"},
            },
            "result": {"summary": 123},
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        inv = out["output"]["error"]["invalid"]
        assert inv == [{"field": "summary", "reason": "must be str"}]

    def test_type_check_list(self):
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": ["findings"],
                "field_types": {"findings": "list"},
            },
            "result": {"findings": "not a list"},
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        assert out["output"]["error"]["invalid"][0]["reason"] == "must be list"

    def test_type_check_absent_field_skipped(self):
        # field declared in field_types but not required and not present:
        # no type error.
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": [],
                "field_types": {"optional_field": "int"},
            },
            "result": {"summary": "x"},
        }
        assert decide(state)["output"]["valid"] is True

    def test_none_value_allowed_when_not_required(self):
        # value None on a non-required typed field passes (source semantics).
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": [],
                "field_types": {"pr_url": "str"},
            },
            "result": {"pr_url": None},
        }
        assert decide(state)["output"]["valid"] is True

    def test_unknown_type_declaration_skipped(self):
        # An unrecognised type name can't be checked → not a violation.
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": ["x"],
                "field_types": {"x": "weirdtype"},
            },
            "result": {"x": object()},
        }
        assert decide(state)["output"]["valid"] is True

    def test_missing_and_invalid_combined(self):
        state = {
            "check": "result",
            "schema_id": "s",
            "schema": {
                "required_fields": ["a", "b"],
                "field_types": {"b": "int"},
            },
            "result": {"b": "wrong"},
        }
        out = decide(state)
        err = out["output"]["error"]
        assert err["missing"] == ["a"]
        assert err["invalid"] == [{"field": "b", "reason": "must be int"}]


# ---------------------------------------------------------------------------
# check == "directive"
# ---------------------------------------------------------------------------

class TestDirective:
    def test_no_non_goals_passes(self):
        state = {
            "check": "directive",
            "contract": {},
            "directive_text": "do anything you like",
        }
        out = decide(state)
        assert out["output"]["valid"] is True
        assert out["self_metric"]["decision_path"] == "no_non_goals"

    def test_empty_directive_passes(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["pricing"]},
            "directive_text": "   ",
        }
        out = decide(state)
        assert out["output"]["valid"] is True
        assert out["self_metric"]["decision_path"] == "empty_directive"

    def test_non_string_directive_passes(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["pricing"]},
            "directive_text": None,
        }
        assert decide(state)["output"]["valid"] is True

    def test_non_goal_match_rejects(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["pricing decisions"]},
            "directive_text": "Please make pricing decisions for the AV Dawson deal",
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["error"] == "directive_conflicts_with_non_goals"
        assert err["matched_non_goals"] == ["pricing decisions"]
        assert "hint" in err
        assert out["self_metric"]["decision_path"] == "non_goal_match"

    def test_case_insensitive_match(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["PRICING"]},
            "directive_text": "set the pricing tier",
        }
        assert decide(state)["output"]["valid"] is False

    def test_no_match_passes(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["pricing", "legal advice"]},
            "directive_text": "draft an outreach email",
        }
        out = decide(state)
        assert out["output"]["valid"] is True
        assert out["self_metric"]["decision_path"] == "no_match"

    def test_multiple_matches_collected(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["pricing", "legal"]},
            "directive_text": "give pricing and legal guidance",
        }
        matched = decide(state)["output"]["error"]["matched_non_goals"]
        assert set(matched) == {"pricing", "legal"}

    def test_blank_non_goal_entries_ignored(self):
        state = {
            "check": "directive",
            "contract": {"non_goals": ["  ", "", None, "pricing"]},
            "directive_text": "no conflict here",
        }
        assert decide(state)["output"]["valid"] is True


# ---------------------------------------------------------------------------
# check == "limits"
# ---------------------------------------------------------------------------

class TestLimits:
    def test_no_limits_passes(self):
        state = {
            "check": "limits",
            "contract": {},
            "daily_items_so_far": 9999,
        }
        out = decide(state)
        assert out["output"]["valid"] is True
        assert out["self_metric"]["decision_path"] == "no_limits"

    def test_within_limits(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_daily_items": 10}},
            "daily_items_so_far": 3,
        }
        assert decide(state)["output"]["valid"] is True

    def test_max_daily_items_hit(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_daily_items": 5}},
            "daily_items_so_far": 5,
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["limit"] == "max_daily_items"
        assert err["current"] == 5
        assert err["configured"] == 5

    def test_max_daily_cost_hit(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_daily_cost_usd": 2.0}},
            "daily_cost_usd_so_far": 2.5,
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        err = out["output"]["error"]
        assert err["limit"] == "max_daily_cost_usd"
        assert err["current"] == 2.5
        assert err["configured"] == 2.0

    def test_cost_rounded(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_daily_cost_usd": 1.0}},
            "daily_cost_usd_so_far": 1.234567,
        }
        assert decide(state)["output"]["error"]["current"] == 1.2346

    def test_max_concurrent_hit(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_concurrent_sessions": 2}},
            "concurrent_sessions": 2,
        }
        out = decide(state)
        assert out["output"]["valid"] is False
        assert out["output"]["error"]["limit"] == "max_concurrent_sessions"

    def test_items_checked_before_cost(self):
        # When both would trip, items is reported first (source ordering).
        state = {
            "check": "limits",
            "contract": {
                "limits": {"max_daily_items": 1, "max_daily_cost_usd": 1.0},
            },
            "daily_items_so_far": 5,
            "daily_cost_usd_so_far": 5.0,
        }
        assert decide(state)["output"]["error"]["limit"] == "max_daily_items"

    def test_zero_limit_blocks_immediately(self):
        state = {
            "check": "limits",
            "contract": {"limits": {"max_daily_items": 0}},
            "daily_items_so_far": 0,
        }
        assert decide(state)["output"]["valid"] is False


# ---------------------------------------------------------------------------
# Dispatch + fail-open + self_metric
# ---------------------------------------------------------------------------

class TestDispatch:
    def test_unknown_check_fail_open(self):
        out = decide({"check": "bogus"})
        assert out["output"]["valid"] is True
        assert out["output"]["error"]["error"] == "unknown_check"
        assert out["self_metric"]["confidence"] == 0.0
        assert out["self_metric"]["decision_path"] == "unknown_check"

    def test_missing_check_fail_open(self):
        out = decide({})
        assert out["output"]["valid"] is True
        assert out["self_metric"]["decision_path"] == "unknown_check"

    def test_context_arg_ignored(self):
        out = decide(
            {"check": "limits", "contract": {}},
            context={"anything": 1},
        )
        assert out["output"]["valid"] is True

    def test_every_output_has_shape(self):
        for state in (
            {"check": "result", "schema_id": "s", "schema": {"required_fields": []}, "result": {}},
            {"check": "directive", "contract": {}, "directive_text": "x"},
            {"check": "limits", "contract": {}},
            {"check": "bogus"},
        ):
            out = decide(state)
            assert set(out.keys()) == {"output", "rationale", "self_metric"}
            assert "valid" in out["output"]
            assert "error" in out["output"]
            assert isinstance(out["rationale"], str)
            assert set(out["self_metric"].keys()) == {"confidence", "decision_path"}


# ---------------------------------------------------------------------------
# CLI / JSON round-trip
# ---------------------------------------------------------------------------

class TestCLI:
    def _run(self, tmp_path, payload):
        p = tmp_path / "in.json"
        p.write_text(json.dumps(payload))
        env = dict(os.environ, ORGAN_INPUT=str(p))
        organ = os.path.join(os.path.dirname(__file__), "organ.py")
        res = subprocess.run(
            [sys.executable, organ],
            env=env,
            capture_output=True,
            text=True,
        )
        return res

    def test_cli_valid(self, tmp_path):
        res = self._run(tmp_path, {"state": {
            "check": "directive", "contract": {}, "directive_text": "hi"}})
        assert res.returncode == 0
        out = json.loads(res.stdout)
        assert out["output"]["valid"] is True

    def test_cli_invalid_input(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        env = dict(os.environ, ORGAN_INPUT=str(p))
        organ = os.path.join(os.path.dirname(__file__), "organ.py")
        res = subprocess.run(
            [sys.executable, organ], env=env, capture_output=True, text=True)
        assert res.returncode == 1
        assert "invalid input" in res.stderr

    def test_samples_round_trip(self):
        """Every committed sample produces a well-shaped output."""
        here = os.path.dirname(__file__)
        sdir = os.path.join(here, "samples")
        names = [f for f in os.listdir(sdir) if f.endswith(".json")]
        assert names, "expected committed samples"
        for name in names:
            payload = json.loads(open(os.path.join(sdir, name)).read())
            out = decide(payload["state"], payload.get("context"))
            assert set(out.keys()) == {"output", "rationale", "self_metric"}


# Pin each committed sample to its expected verdict + decision_path. The
# round-trip test above only checks output *shape* — a logic regression that
# flipped a verdict (e.g. stopped matching non_goals) would still produce a
# well-shaped output and slip through. This locks the actual decision so the
# conformance Action's shadow-run reflects intended behaviour, not just a
# parseable envelope. (Lesson: pin the action, not just the shape.)
_SAMPLE_EXPECTATIONS = {
    "result_valid.json": {"valid": True, "decision_path": "schema_ok"},
    "result_schema_violation.json": {
        "valid": False,
        "decision_path": "schema_violation",
    },
    "directive_conflicts_non_goal.json": {
        "valid": False,
        "decision_path": "non_goal_match",
    },
    "limits_exceeded.json": {
        "valid": False,
        "decision_path": "max_daily_items",
    },
}


class TestSamplesConform:
    def test_every_sample_is_pinned(self):
        """Each committed sample has an expectation (so a new sample can't
        be added without also pinning its verdict)."""
        here = os.path.dirname(__file__)
        sdir = os.path.join(here, "samples")
        names = {f for f in os.listdir(sdir) if f.endswith(".json")}
        assert names == set(_SAMPLE_EXPECTATIONS), (
            "every sample must be pinned in _SAMPLE_EXPECTATIONS; "
            f"samples={sorted(names)} pinned={sorted(_SAMPLE_EXPECTATIONS)}"
        )

    def test_samples_produce_expected_verdict(self):
        here = os.path.dirname(__file__)
        sdir = os.path.join(here, "samples")
        for name, expected in _SAMPLE_EXPECTATIONS.items():
            payload = json.loads(open(os.path.join(sdir, name)).read())
            out = decide(payload["state"], payload.get("context"))
            assert out["output"]["valid"] is expected["valid"], (
                f"{name}: valid {out['output']['valid']} != "
                f"{expected['valid']}"
            )
            assert out["self_metric"]["decision_path"] == expected[
                "decision_path"
            ], (
                f"{name}: decision_path {out['self_metric']['decision_path']}"
                f" != {expected['decision_path']}"
            )
            # Rejections must carry an error body; acceptances must not.
            if expected["valid"]:
                assert out["output"]["error"] is None
            else:
                assert isinstance(out["output"]["error"], dict)


# ---------------------------------------------------------------------------
# Connection standard — ports.json conformance (orchestrator/CONNECTORS.md)
# ---------------------------------------------------------------------------

from ports_check import check_ports, _TrackingState


class TestPorts:
    def test_ports_json_parses_and_conforms(self):
        # check_ports() raises AssertionError on any conformance failure:
        # ports.json parses, every type is in the vocabulary, and decide()
        # reads each declared input / writes each declared output.
        summary = check_ports()
        assert summary["inputs"] == ["schema", "result", "contract"]
        assert summary["outputs"] == ["valid", "error"]
        assert summary["samples_checked"] >= 1

    def test_declared_types_all_in_vocab(self):
        ports = json.load(open(os.path.join(os.path.dirname(__file__), "ports.json")))
        vocab = json.load(open(os.path.join(os.path.dirname(__file__), "types.json")))
        names = set(vocab["types"])
        for side in ("inputs", "outputs"):
            for p in ports[side]:
                assert p["type"] in names, f"{p['type']} missing from types.json"

    def test_all_inputs_optional_for_op_dispatch(self):
        ports = json.load(open(os.path.join(os.path.dirname(__file__), "ports.json")))
        # Op-dispatched on state['check']; no input is present in every mode.
        for p in ports["inputs"]:
            assert p["required"] is False

    def test_tracking_state_witnesses_reads(self):
        # The read check must witness a real access, not a textual match.
        ts = _TrackingState({"check": "result", "schema": {"required_fields": []},
                             "result": {}, "schema_id": "s"})
        decide(ts)
        assert "schema" in ts.touched
        assert "result" in ts.touched

    def test_outputs_written_on_every_dispatch(self):
        # Each declared output name appears under output for every mode.
        for state in (
            {"check": "result", "schema_id": "s", "schema": {"required_fields": []}, "result": {}},
            {"check": "directive", "contract": {}, "directive_text": "x"},
            {"check": "limits", "contract": {}},
        ):
            body = decide(state)["output"]
            assert "valid" in body and "error" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
