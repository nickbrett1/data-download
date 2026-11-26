"""The interpreter over a closed vocabulary (§3), driven by a stub driver."""

from __future__ import annotations

import json

import pytest

from conftest import PLAN_BODY
from data_download import interpreter
from data_download.profiles import market_data_window as databento

VERIFY = {"on_failure": "quarantine"}


def _job(**verify):
    return databento.bind(PLAN_BODY["params"], verify or VERIFY)


class StubDriver:
    """A driver that answers, records what it was asked, and touches nothing.

    Deliberately implements the whole protocol: the point of the interface is
    that the interpreter calls *operations*, so a stub missing one would let a
    step kind pass by accident.
    """

    def __init__(self, manifest=None, staged=None, claimed=None) -> None:
        self.calls: list[str] = []
        self.manifest = manifest if manifest is not None else []
        self.staged = staged if staged is not None else {}
        self.claimed = claimed if claimed is not None else [e["name"] for e in self.manifest]

    def cost(self, **kwargs):
        self.calls.append("cost")
        return 12.34

    def resolve(self, **kwargs):
        self.calls.append("resolve")
        return {"symbols": list(kwargs.get("symbols") or [])}

    def submit(self, **kwargs):
        self.calls.append("submit")
        return {"vendor_job_id": "vjob_1"}

    def poll(self, **kwargs):
        self.calls.append("poll")
        return {"state": "done"}

    def list_files(self, **kwargs):
        self.calls.append("list_files")
        return list(self.manifest)

    def fetch(self, **kwargs):
        self.calls.append("fetch")
        return sorted(self.staged)

    def convert(self, **kwargs):
        self.calls.append("convert")
        return sorted(self.staged)

    def verify(self, **kwargs):
        self.calls.append("verify")
        return {"claimed": self.claimed, "manifest": self.manifest, "staged": self.staged}


def _entry(name="a.dbn.zst", sha="s", rows=2):
    return {"name": name, "sha256": sha, "rows": rows}


def test_the_whole_job_runs_and_calls_one_operation_per_kind():
    manifest = [_entry()]
    driver = StubDriver(manifest=manifest, staged={"a.dbn.zst": {"sha256": "s", "rows": 2}})
    result = interpreter.run(_job(), driver)
    assert driver.calls == [
        "resolve", "cost", "submit", "poll", "list_files", "fetch", "convert", "verify",
    ]
    assert result.steps == (
        "resolve", "quote", "submit", "poll", "download", "convert", "verify",
    )
    assert result.verdict == interpreter.PASS
    assert result.disposition == interpreter.COMMIT
    assert result.interpreter_version == interpreter.INTERPRETER_VERSION


def test_the_steps_are_announced_as_they_start():
    """Progress is a notification, in order, before the work rather than after.

    A caller that prints "step 6 of 7" only once step 6 has finished is telling
    the reader where the run has *been*, which is the thing a live page already
    knows.
    """
    driver = StubDriver(
        manifest=[_entry()], staged={"a.dbn.zst": {"sha256": "s", "rows": 2}}
    )
    seen: list[tuple[int, str, int]] = []
    interpreter.run(_job(), driver, lambda index, kind, total: seen.append((index, kind, total)))
    assert seen == [
        (0, "resolve", 7),
        (1, "quote", 7),
        (2, "submit", 7),
        (3, "poll", 7),
        (4, "download", 7),
        (5, "convert", 7),
        (6, "verify", 7),
    ]


def test_a_progress_note_that_throws_does_not_cost_the_run():
    """The run is the point. A caller's bookkeeping is not allowed to stop it."""
    driver = StubDriver(
        manifest=[_entry()], staged={"a.dbn.zst": {"sha256": "s", "rows": 2}}
    )

    def angry(index, kind, total):
        raise RuntimeError(f"no note for {kind}")

    result = interpreter.run(_job(), driver, angry)
    assert result.verdict == interpreter.PASS
    assert driver.calls[0] == "resolve"


def test_every_kind_gets_its_own_operation():
    """One function per kind, shared by every Plan: the interpreter owns the flow."""
    for kind, operation in [
        ("resolve", "resolve"),
        ("quote", "cost"),
        ("submit", "submit"),
        ("poll", "poll"),
        ("download", "list_files"),
        ("convert", "convert"),
        ("verify", "verify"),
    ]:
        assert kind in json.dumps(_job()), kind
        assert hasattr(StubDriver(), operation)


def test_an_unknown_kind_is_refused_by_the_interpreter_too():
    """The gate refuses it first; the interpreter fails closed if it ever sees one."""
    job = {"plan": "p", "steps": [{"kind": "transfer", "kwargs": {}}]}
    with pytest.raises(interpreter.InterpreterError) as caught:
        interpreter.run(job, StubDriver())
    assert "unknown step kind" in str(caught.value)


def test_a_job_with_no_verify_cannot_produce_a_verdict():
    job = {"plan": "p", "steps": [{"kind": "resolve", "kwargs": {"dataset": "d"}}]}
    with pytest.raises(interpreter.InterpreterError, match="no verify step"):
        interpreter.run(job, StubDriver())


def test_a_failed_vendor_job_is_an_error_not_a_verdict():
    class Failed(StubDriver):
        def poll(self, **kwargs):
            return {"state": "errored"}

    with pytest.raises(interpreter.InterpreterError, match="errored"):
        interpreter.run(_job(), Failed())


def test_polling_is_bounded():
    class Forever(StubDriver):
        def poll(self, **kwargs):
            return {"state": "running"}

    with pytest.raises(interpreter.InterpreterError, match=str(interpreter.MAX_POLLS)):
        interpreter.run(_job(), Forever())


def test_submit_without_a_vendor_job_id_is_an_error():
    class NoId(StubDriver):
        def submit(self, **kwargs):
            return {}

    with pytest.raises(interpreter.InterpreterError, match="vendor_job_id"):
        interpreter.run(_job(), NoId())


# -- the three verdicts --------------------------------------------------------


def test_absent_file_is_a_contradiction():
    manifest = [_entry()]
    verdict = interpreter.classify({"claimed": ["a.dbn.zst"], "manifest": manifest, "staged": {}})
    assert verdict.verdict == interpreter.CONTRADICTION
    assert verdict.disposition == interpreter.QUARANTINE
    assert verdict.detail["problems"][0]["problem"] == "absent"


def test_sha_mismatch_is_a_contradiction():
    manifest = [_entry()]
    staged = {"a.dbn.zst": {"sha256": "other", "rows": 2}}
    verdict = interpreter.classify({"claimed": ["a.dbn.zst"], "manifest": manifest, "staged": staged})
    assert verdict.verdict == interpreter.CONTRADICTION
    assert verdict.detail["problems"][0]["problem"] == "sha256 mismatch"


def test_row_count_off_the_manifest_is_a_contradiction():
    """Compared against the manifest's count, never against the plan's expectation."""
    manifest = [_entry(rows=2)]
    staged = {"a.dbn.zst": {"sha256": "s", "rows": 1}}
    verdict = interpreter.classify({"claimed": ["a.dbn.zst"], "manifest": manifest, "staged": staged})
    assert verdict.verdict == interpreter.CONTRADICTION
    assert verdict.detail["problems"][0]["problem"] == "row count mismatch"


def test_an_unclaimed_file_is_a_contradiction():
    manifest = [_entry()]
    staged = {"a.dbn.zst": {"sha256": "s", "rows": 2}, "extra.dbn.zst": {"sha256": "s", "rows": 2}}
    verdict = interpreter.classify({"claimed": ["a.dbn.zst"], "manifest": manifest, "staged": staged})
    assert verdict.verdict == interpreter.CONTRADICTION
    assert any(p["problem"] == "not in the manifest" for p in verdict.detail["problems"])


def test_a_manifest_publishing_less_than_claimed_is_a_shortfall():
    """The distinction §3.2 exists for: valid data, less than asked for."""
    manifest = [_entry(name="a.dbn.zst")]
    staged = {"a.dbn.zst": {"sha256": "s", "rows": 2}}
    verdict = interpreter.classify(
        {"claimed": ["a.dbn.zst", "b.dbn.zst"], "manifest": manifest, "staged": staged},
        "commit_marked",
    )
    assert verdict.verdict == interpreter.SHORTFALL
    assert verdict.disposition == interpreter.COMMIT_MARKED
    assert verdict.detail["missing"] == ["b.dbn.zst"]


def test_a_shortfall_under_a_quarantining_plan_quarantines():
    """A Job cannot widen past what its Plan declared (§5.4)."""
    manifest = [_entry(name="a.dbn.zst")]
    verdict = interpreter.classify(
        {"claimed": ["a.dbn.zst", "b.dbn.zst"], "manifest": manifest,
         "staged": {"a.dbn.zst": {"sha256": "s", "rows": 2}}},
        "quarantine",
    )
    assert verdict.verdict == interpreter.SHORTFALL
    assert verdict.disposition == interpreter.QUARANTINE


def test_agreement_is_a_pass():
    manifest = [_entry()]
    staged = {"a.dbn.zst": {"sha256": "s", "rows": 2}}
    verdict = interpreter.classify({"claimed": ["a.dbn.zst"], "manifest": manifest, "staged": staged})
    assert verdict.verdict == interpreter.PASS
    assert verdict.disposition == interpreter.COMMIT


@pytest.mark.parametrize(
    "evidence",
    [None, {}, {"manifest": "not-a-list", "staged": {}}, {"manifest": [{"rows": 1}], "staged": {}}],
    ids=["none", "empty", "manifest-not-a-list", "entry-without-a-name"],
)
def test_a_broken_verifier_is_a_verifier_error_and_quarantines(evidence):
    verdict = interpreter.classify(evidence)
    assert verdict.verdict == interpreter.VERIFIER_ERROR
    assert verdict.disposition == interpreter.QUARANTINE


def test_a_raising_driver_does_not_become_a_verdict():
    """A verifier that throws is a crash of the run, not a classification."""

    class Broken(StubDriver):
        def verify(self, **kwargs):
            raise RuntimeError("manifest was HTML")

    with pytest.raises(RuntimeError, match="HTML"):
        interpreter.run(_job(), Broken())


def test_the_driver_reports_evidence_and_the_interpreter_classifies():
    """The driver returns the three quantities; `classify` is the only judge."""
    driver = StubDriver(
        manifest=[_entry()], staged={"a.dbn.zst": {"sha256": "s", "rows": 2}}
    )
    evidence = driver.verify(dest="files/")
    assert set(evidence) == {"claimed", "manifest", "staged"}
    assert "verdict" not in evidence
    assert interpreter.classify(evidence).verdict == interpreter.PASS


def test_the_quote_is_recorded_but_never_compared_to_an_estimate():
    driver = StubDriver(manifest=[_entry()], staged={"a.dbn.zst": {"sha256": "s", "rows": 2}})
    result = interpreter.run(_job(), driver)
    assert result.detail["quote_usd"] == 12.34
    # And nothing anywhere claims the estimate is that number.
    assert "estimate_usd" not in json.dumps(result.as_dict())
