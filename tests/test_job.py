"""The Job document: canonical bytes, the closed vocabulary, and order (§2, §3)."""

from __future__ import annotations

import json

import pytest

from conftest import PLAN_BODY
from data_download import job as job_mod
from data_download.profiles import market_data_window as databento


def _job(**overrides):
    bound = databento.bind(PLAN_BODY["params"], {"on_failure": "quarantine"})
    bound.update(overrides)
    return bound


def test_canonical_bytes_are_sorted_indented_and_end_with_one_newline():
    text = job_mod.canonical({"steps": [{"kwargs": {}, "kind": "poll"}], "plan": "p"})
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert text.index('"plan"') < text.index('"steps"')  # sorted keys
    assert "\n  " in text  # indented, because a human reads the diff


def test_key_order_in_the_binder_output_does_not_change_the_bytes():
    forwards = databento.bind(PLAN_BODY["params"], {"on_failure": "quarantine"})
    backwards = dict(reversed(list(forwards.items())))
    assert job_mod.canonical(forwards) == job_mod.canonical(backwards)
    assert job_mod.job_hash(job_mod.canonical(forwards)) == job_mod.job_hash(
        job_mod.canonical(backwards)
    )


def test_the_hash_covers_the_stored_bytes_not_the_object():
    text = job_mod.canonical(_job())
    assert job_mod.job_hash(text) == job_mod.job_hash(text)
    # A reformatted copy of the same data is a different document, and the hash
    # says so -- which is why nothing may re-canonicalise before comparing.
    assert job_mod.job_hash(text) != job_mod.job_hash(text.rstrip("\n"))


def test_the_batch_job_is_the_seven_kinds_in_order():
    kinds = [s["kind"] for s in _job()["steps"]]
    assert kinds == [
        "resolve", "quote", "submit", "poll", "download", "convert", "verify",
    ]
    assert set(kinds) <= set(job_mod.STEP_KINDS)


def test_binding_is_deterministic():
    a = job_mod.canonical(databento.bind(PLAN_BODY["params"], {"on_failure": "quarantine"}))
    b = job_mod.canonical(databento.bind(PLAN_BODY["params"], {"on_failure": "quarantine"}))
    assert a == b


def test_a_different_param_binds_a_different_job():
    other = dict(PLAN_BODY["params"], symbols=["NQ.c.0"])
    a = job_mod.canonical(databento.bind(PLAN_BODY["params"], {"on_failure": "quarantine"}))
    b = job_mod.canonical(databento.bind(other, {"on_failure": "quarantine"}))
    assert a != b


def test_on_failure_reaches_the_job():
    job = databento.bind(PLAN_BODY["params"], {"on_failure": "commit_marked"})
    assert job["steps"][-1]["kwargs"]["on_failure"] == "commit_marked"


def test_a_binder_without_a_verify_policy_refuses():
    with pytest.raises(ValueError, match="on_failure"):
        databento.bind(PLAN_BODY["params"], None)


def test_unknown_kind_is_refused_naming_the_index_and_the_kind():
    job = _job()
    job["steps"][4] = {"kind": "transfer", "kwargs": {}}
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    assert caught.value.step_index == 4
    assert caught.value.kind == "transfer"
    assert "closed" in str(caught.value)


def test_unknown_top_level_key_is_refused():
    job = _job()
    job["entrypoint"] = "data_download.profiles.market_data_window:bind"
    with pytest.raises(job_mod.JobInvalid, match="unknown top-level"):
        job_mod.validate(job)


def test_a_step_carries_kind_and_kwargs_and_nothing_else():
    job = _job()
    job["steps"][0]["module"] = "vendor.sdk"
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    assert caught.value.step_index == 0


def test_a_plan_name_that_is_a_path_is_refused():
    job = _job(plan="../profiles/other")
    with pytest.raises(job_mod.JobInvalid, match="never a path"):
        job_mod.validate(job)


def test_an_empty_step_list_is_refused():
    with pytest.raises(job_mod.JobInvalid, match="non-empty"):
        job_mod.validate({"plan": "p", "steps": []})


def test_poll_before_submit_is_refused():
    job = _job()
    job["steps"] = [
        {"kind": "poll", "kwargs": {}},
        {"kind": "submit", "kwargs": {}},
    ]
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    assert caught.value.kind == "poll"
    assert "submit" in str(caught.value)


def test_convert_before_download_is_refused():
    job = _job()
    job["steps"] = [
        {"kind": "download", "kwargs": {"dest": "files/"}},
        {"kind": "convert", "kwargs": {}},
        {"kind": "download", "kwargs": {"dest": "files/"}},
    ]
    # The first convert follows a download, so this one is legal; move it up to
    # prove the rule fires rather than being vacuous.
    assert job_mod.validate(job)
    illegal = {"plan": "p", "steps": [
        {"kind": "convert", "kwargs": {}},
        {"kind": "download", "kwargs": {"dest": "files/"}},
    ]}
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(illegal)
    assert caught.value.step_index == 0
    assert caught.value.kind == "convert"


def test_verify_must_be_last():
    job = _job()
    steps = job["steps"]
    # Everything verify needs is before it; it is still not the last step, which
    # is the rule being tested.
    job["steps"] = [*steps[:-2], steps[-1], steps[-2]]
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    assert caught.value.kind == "verify"
    assert "final step" in str(caught.value)


def test_two_verifies_are_refused():
    job = _job()
    job["steps"] = [*job["steps"], {"kind": "verify", "kwargs": {"on_failure": "quarantine"}}]
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    assert caught.value.kind == "verify"
    assert "at most once" in str(caught.value)


def test_a_job_without_verify_is_valid_but_the_interpreter_will_not_run_it():
    """The vocabulary does not force verify; interpretability does (§3)."""
    job = {"plan": "p", "steps": [{"kind": "resolve", "kwargs": {}}]}
    assert job_mod.validate(job) is job


def test_refusal_reaches_the_api_as_422_plan_not_valid():
    job = _job()
    job["steps"][1] = {"kind": "price", "kwargs": {}}
    with pytest.raises(job_mod.JobInvalid) as caught:
        job_mod.validate(job)
    api = caught.value.as_api_error()
    assert api.status == 422
    assert api.code == "plan_not_valid"
    assert api.extra["step_index"] == 1
    assert api.extra["kind"] == "price"
    assert json.dumps(api.envelope())  # serialisable, so a client can branch
