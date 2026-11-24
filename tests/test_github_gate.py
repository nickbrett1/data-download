"""The gate reads *both* of GitHub's CI vocabularies.

This exists because the acceptance run proved the old one wrong: the drivers
repo's CI is Buildkite, which publishes a commit **status** and never a check
run, so a gate that read only `check-runs` answered "no check runs for this
commit" on every job -- a structural stop, not a red build, and nothing the
loop could ever get past.
"""

from __future__ import annotations

import pytest

from data_download import github_api


def _answer(monkeypatch, *, runs=None, statuses=None):
    """Answer the two endpoints the gate reads, and nothing else."""

    def fake(method, path, *, token, body=None, raw=False, timeout=60.0):
        assert method == "GET"
        if path.endswith("/check-runs"):
            return {"check_runs": runs or []}
        if path.endswith("/status"):
            return {"state": "whatever", "statuses": statuses or []}
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(github_api, "_call", fake)


def test_a_buildkite_status_is_the_gate(monkeypatch):
    """The case that stopped the acceptance run: a status, no check run."""
    _answer(
        monkeypatch,
        statuses=[
            {"context": "buildkite/data-download-drivers", "state": "success"},
        ],
    )
    verdict = github_api.gate("o/r", "a" * 40, token="t")
    assert verdict["ok"] is True
    assert "1 commit status(es)" in verdict["detail"]


def test_a_pending_status_is_a_wait_not_a_refusal(monkeypatch):
    _answer(monkeypatch, statuses=[{"context": "buildkite/x", "state": "pending"}])
    verdict = github_api.gate("o/r", "a" * 40, token="t")
    assert verdict["ok"] is False
    assert verdict["pending"] is True
    assert "buildkite/x (pending)" in verdict["detail"]


@pytest.mark.parametrize("state", ["failure", "error"])
def test_a_red_status_is_a_stop(monkeypatch, state):
    _answer(monkeypatch, statuses=[{"context": "buildkite/x", "state": state}])
    verdict = github_api.gate("o/r", "a" * 40, token="t")
    assert verdict["ok"] is False
    assert not verdict.get("pending")
    assert f"buildkite/x: {state}" in verdict["detail"]


def test_check_runs_still_count_and_both_are_read(monkeypatch):
    _answer(
        monkeypatch,
        runs=[{"name": "build", "status": "completed", "conclusion": "success"}],
        statuses=[{"context": "buildkite/x", "state": "success"}],
    )
    verdict = github_api.gate("o/r", "a" * 40, token="t")
    assert verdict["ok"] is True
    assert "1 check run(s) and 1 commit status(es)" in verdict["detail"]


def test_nothing_at_all_is_not_green(monkeypatch):
    """Untested and green are not the same answer."""
    _answer(monkeypatch)
    verdict = github_api.gate("o/r", "a" * 40, token="t")
    assert verdict["ok"] is False
    assert not verdict.get("pending")
