"""The mechanical gate (§4): parse -> bind -> vocabulary -> byte-stability.

Four assertions in order, and a failure at any one stops the gate. The negative
cases are the point of having a gate at all, so they assert a **failure** --
never a skip.

Everything here runs inside the generated `build` step (`pytest -q`). There is no
bespoke Buildkite step to add: genproj exposes three config keys and no `steps`
(`[[genproj-capability-gaps-data-download]]` Gap 1), so the gate lands as a
pytest suite named so its future promotion is obvious.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from conftest import PLAN_BODY, PLAN_TYPES_DIR
from data_download import job as job_mod
from data_download import profiles
from data_download.models import validate_plan
from data_download.profiles import market_data_window as databento
from data_download.registry import load_registry

VERIFY = {"on_failure": "quarantine"}


@pytest.fixture(scope="module")
def registry():
    return load_registry(PLAN_TYPES_DIR)


# -- 1. parse ------------------------------------------------------------------


def test_parse_the_params_are_a_valid_instance_of_the_plans_schema(registry):
    plan = profiles.resolve(registry, "market_data_window")
    validated = validate_plan(PLAN_BODY, plan.type)
    assert validated["params"] == PLAN_BODY["params"]


def test_parse_a_missing_required_field_fails_and_names_it(registry):
    from data_download import errors

    plan = profiles.resolve(registry, "market_data_window")
    body = json.loads(json.dumps(PLAN_BODY))
    del body["params"]["symbols"]
    with pytest.raises(errors.ApiError) as caught:
        validate_plan(body, plan.type)
    assert caught.value.status == 422
    # JSON Schema reports a *missing* property on the object, so the path is the
    # object and the field is named in the message. A bad value names its path.
    assert caught.value.extra.get("field") == "params"
    assert "symbols" in caught.value.message


def test_parse_a_bad_value_fails_naming_the_field_path(registry):
    from data_download import errors

    plan = profiles.resolve(registry, "market_data_window")
    body = json.loads(json.dumps(PLAN_BODY))
    body["params"]["stype_in"] = "guessed"
    with pytest.raises(errors.ApiError) as caught:
        validate_plan(body, plan.type)
    assert caught.value.extra.get("field") == "params.stype_in"
    assert "params.stype_in" in caught.value.message


# -- 2. bind -------------------------------------------------------------------


def test_bind_returns_a_job_that_validates_against_the_step_schema(registry):
    plan = profiles.resolve(registry, "market_data_window")
    bound = plan.binder(PLAN_BODY["params"], VERIFY)
    assert job_mod.validate(bound) is bound


def test_every_registry_entry_has_a_committed_binder(registry):
    """A Plan declared in YAML with no binder advertises what nothing can bind."""
    assert profiles.unbound_types(registry) == []


def test_a_registered_plan_without_a_binder_is_a_code_defect(registry, monkeypatch):
    monkeypatch.setattr(profiles, "BINDERS", {})
    with pytest.raises(RuntimeError, match="names binder"):
        profiles.resolve(registry, "market_data_window")


def test_an_unregistered_plan_resolves_to_none_so_the_api_can_refuse_it(registry):
    assert profiles.resolve(registry, "not_a_plan") is None
    assert profiles.resolve(registry, None) is None


# -- 3. vocabulary -------------------------------------------------------------


def test_every_step_kind_is_in_the_closed_set(registry):
    job = databento.bind(PLAN_BODY["params"], VERIFY)
    for index, step in enumerate(job["steps"]):
        assert step["kind"] in job_mod.STEP_KIND_SET, (index, step["kind"])


def test_a_hand_edited_kind_fails_naming_the_index_and_kind(tmp_path):
    """The assertion the gate exists for: edit one kind, watch it stop the run."""
    text, _ = job_mod.bind_and_seal(databento.bind(PLAN_BODY["params"], VERIFY))
    path = tmp_path / "job.json"
    path.write_text(text.replace('"kind": "download"', '"kind": "transfer"'))

    loaded = json.loads(path.read_text())
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(loaded)
    assert caught.value.kind == "transfer"
    assert caught.value.step_index == 4
    assert "steps[4]" in caught.value.message


def test_a_job_missing_verify_fails_at_the_interpreter_not_the_vocabulary():
    """The vocabulary permits it; the interpreter refuses to call it a landing."""
    from data_download import interpreter

    job = {"plan": "p", "steps": [{"kind": "resolve", "kwargs": {}}]}
    job_mod.validate(job)

    class Dummy:
        def resolve(self, **kwargs):
            return {}

    with pytest.raises(interpreter.InterpreterError, match="no verify step"):
        interpreter.run(job, Dummy())


def test_verify_on_failure_outside_the_plans_set_is_refused(registry):
    from data_download import errors

    plan = profiles.resolve(registry, "market_data_window")
    body = json.loads(json.dumps(PLAN_BODY))
    body["verify"] = {"on_failure": "ignore"}
    with pytest.raises(errors.ApiError) as caught:
        validate_plan(body, plan.type)
    assert caught.value.code == "plan_not_valid"


def test_a_binder_returning_a_rejected_job_fails_rather_than_skips(registry, monkeypatch):
    """No `pytest.skip`: a broken binder must make the gate red."""
    plan = profiles.resolve(registry, "market_data_window")

    def broken(params, verify):
        return {"plan": plan.name, "steps": [{"kind": "transfer", "kwargs": {}}]}

    monkeypatch.setitem(profiles.BINDERS, plan.name, broken)
    bound = profiles.resolve(registry, plan.name).binder(PLAN_BODY["params"], VERIFY)
    with pytest.raises(job_mod.JobInvalid):
        job_mod.bind_and_seal(bound)


# -- 4. byte-stability ---------------------------------------------------------


def test_binding_is_byte_identical_across_calls_in_one_process(tmp_path):
    first = tmp_path / "one.json"
    second = tmp_path / "two.json"
    first.write_bytes(job_mod.canonical(databento.bind(PLAN_BODY["params"], VERIFY)).encode())
    second.write_bytes(job_mod.canonical(databento.bind(PLAN_BODY["params"], VERIFY)).encode())
    # Compared as bytes read from disk: comparing parsed objects would test the
    # comparison, not the binder.
    assert first.read_bytes() == second.read_bytes()


def _bind_in_a_fresh_process(params: dict) -> bytes:
    code = (
        "import sys;"
        "from data_download import job;"
        "from data_download.profiles import market_data_window as d;"
        f"sys.stdout.write(job.canonical(d.bind({params!r}, "
        "{'on_failure': 'quarantine'})))"
    )
    finished = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=True, text=True
    )
    return finished.stdout.encode()


def test_binding_is_byte_identical_across_processes(tmp_path):
    in_process = tmp_path / "in-process.json"
    in_process.write_bytes(job_mod.canonical(databento.bind(PLAN_BODY["params"], VERIFY)).encode())

    other = tmp_path / "other-process.json"
    other.write_bytes(_bind_in_a_fresh_process(PLAN_BODY["params"]))

    assert in_process.read_bytes() == other.read_bytes()
    # And the hash agrees, because it is a hash of these bytes.
    assert job_mod.job_hash(in_process.read_text()) == job_mod.job_hash(other.read_text())


def test_the_same_params_and_the_same_verify_bind_the_same_hash_after_a_json_round_trip():
    original = databento.bind(PLAN_BODY["params"], VERIFY)
    round_tripped = json.loads(json.dumps(original))
    assert job_mod.canonical(original) == job_mod.canonical(round_tripped)


# -- what the gate deliberately does not do ------------------------------------


def test_the_gate_asserts_nothing_about_price():
    """§5.2 removed price verification; a gate that re-adds it is the same mistake."""
    text, _ = job_mod.bind_and_seal(databento.bind(PLAN_BODY["params"], VERIFY))
    assert "estimate_usd" not in text
    job = json.loads(text)
    quote = [s for s in job["steps"] if s["kind"] == "quote"][0]
    assert set(quote["kwargs"]) == {"dataset", "schema", "symbols", "stype_in", "start", "end"}
