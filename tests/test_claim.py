"""The claim is one statement, and the lease is what a CLI cannot have (§8.2).

Phase 3's whole job is that a request *moves*: `approved` -> `running` by a CAS,
and back to `approved` either by `release` or by a lease running out. Nothing
here executes the Job, so nothing here can spend.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import INTENT_BODY, PLAN_BODY, REPO_ROOT, auth, iso, seed
from data_download import coderef
from data_download.db import ClaimRefused, Store, shift_seconds


def _run(coro):
    return asyncio.run(coro)


def _store(path: Path) -> Store:
    store = Store(str(path))
    _run(store.connect())
    return store


async def _call(path: Path, method: str, *args, **kwargs):
    """One coroutine per call: never nest `asyncio.run` inside another loop."""
    store = Store(str(path))
    await store.connect()
    try:
        return await getattr(store, method)(*args, **kwargs)
    finally:
        await store.close()


async def _exec(path: Path, sql: str, params: tuple = ()) -> None:
    store = Store(str(path))
    await store.connect()
    try:
        # autocommit (isolation_level=None): the statement is the transaction
        await store.conn.execute(sql, params)
    finally:
        await store.close()


def _file(client, body=None) -> str:
    response = client.post("/v1/requests", json=body or PLAN_BODY, headers=auth())
    assert response.status_code == 202, response.text
    return response.json()["request_id"]


def _approved(client, settings, actor: str = "nick") -> str:
    request_id = _file(client)
    _run(_call(settings.db_path, "approve", request_id, actor))
    return request_id


def _events(path: Path, request_id: str) -> list[dict]:
    return _run(_call(path, "events_for", request_id))


def test_the_claim_takes_the_row_and_writes_one_runner_event(client, settings):
    request_id = _approved(client, settings)
    row = _run(_call(settings.db_path, "claim", request_id, "host:1:abc"))

    assert row["state"] == "running"
    assert row["owner"] == "host:1:abc"
    assert row["lease_until"] is not None

    claimed = [e for e in _events(settings.db_path, request_id) if e["event"] == "claimed"]
    assert len(claimed) == 1
    assert claimed[0]["actor_kind"] == "runner"
    assert json.loads(claimed[0]["detail_json"])["owner"] == "host:1:abc"


def test_two_concurrent_claims_leave_exactly_one_winner(client, settings):
    """The reason the claim is a CAS and not a read followed by a write."""
    request_id = _approved(client, settings)

    async def go():
        first, second = Store(str(settings.db_path)), Store(str(settings.db_path))
        await first.connect()
        await second.connect()
        try:
            return await asyncio.gather(
                first.claim(request_id, "owner-a"),
                second.claim(request_id, "owner-b"),
                return_exceptions=True,
            )
        finally:
            await first.close()
            await second.close()

    results = _run(go())
    winners = [r for r in results if isinstance(r, dict)]
    refusals = [r for r in results if isinstance(r, ClaimRefused)]
    assert len(winners) == 1, results
    assert len(refusals) == 1, results
    assert refusals[0].reason in {"leased", "not_found"} or refusals[0].state == "running"

    events = [e for e in _events(settings.db_path, request_id) if e["event"] == "claimed"]
    assert len(events) == 1
    stored = _run(_call(settings.db_path, "get_request", request_id))
    assert stored["owner"] == winners[0]["owner"]


def test_a_fresh_lease_refuses_a_second_claimer(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a"))

    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", request_id, "owner-b"))
    assert caught.value.reason == "leased"
    assert caught.value.lease_until is not None


def test_an_expired_lease_is_reclaimed_by_the_same_statement(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a", lease_seconds=1))

    later = shift_seconds(iso(), 5)
    row = _run(_call(settings.db_path, "claim", request_id, "owner-b", now=later))
    assert row["state"] == "running"
    assert row["owner"] == "owner-b"

    claims = [e for e in _events(settings.db_path, request_id) if e["event"] == "claimed"]
    assert len(claims) == 2  # the reclaim is a claim, not a repair


def test_a_runner_may_reclaim_its_own_expired_lease(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a", lease_seconds=1))

    row = _run(
        _call(
            settings.db_path,
            "claim",
            request_id,
            "owner-a",
            now=shift_seconds(iso(), 5),
        )
    )
    assert row["owner"] == "owner-a"


def test_a_draft_is_refused_with_its_reason(client, settings):
    request_id = _file(client)
    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", request_id, "owner-a"))
    assert caught.value.reason == "draft"
    assert caught.value.state == "draft"


def test_a_blocked_row_is_refused(client, settings):
    request_id = _file(client, INTENT_BODY)
    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", request_id, "owner-a"))
    assert caught.value.reason == "blocked"


def test_a_settled_row_is_refused(client, settings):
    seed(settings.db_path, [{"id": "req_done", "state": "succeeded", "job_json": "{}"}])
    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", "req_done", "owner-a"))
    assert caught.value.reason == "settled"


def test_an_unknown_id_is_not_found(client, settings):
    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", "req_nope", "owner-a"))
    assert caught.value.reason == "not_found"


def test_release_returns_the_row_without_waiting_out_the_lease(client, settings):
    """A runner that fails its first step in a second must not be an outage."""
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a"))

    assert _run(_call(settings.db_path, "release", request_id, "owner-a", reason="step failed"))
    row = _run(_call(settings.db_path, "get_request", request_id))
    assert row["state"] == "approved"
    assert row["owner"] is None
    assert row["lease_until"] is None

    released = [e for e in _events(settings.db_path, request_id) if e["event"] == "released"]
    assert len(released) == 1
    assert json.loads(released[0]["detail_json"])["reason"] == "step failed"


def test_release_by_a_non_owner_is_refused(client, settings):
    """A runner that lost its lease must not release the new claimant's row."""
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a"))

    assert _run(_call(settings.db_path, "release", request_id, "owner-b")) is False
    row = _run(_call(settings.db_path, "get_request", request_id))
    assert row["owner"] == "owner-a"
    assert row["state"] == "running"


def test_v_stuck_reports_an_expired_lease_only_once_it_has_expired(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "owner-a", lease_seconds=600))

    stuck = _run(_call(settings.db_path, "view", "v_stuck"))
    assert [r for r in stuck if r["condition"] == "lease_expired"] == []

    # Rewind the lease into the past rather than sleeping for it.
    _run(_exec(settings.db_path, "UPDATE requests SET lease_until = ? WHERE id = ?",
               (shift_seconds(iso(), -60), request_id)))
    stuck = _run(_call(settings.db_path, "view", "v_stuck"))
    expired = [r for r in stuck if r["condition"] == "lease_expired"]
    assert [r["request_id"] for r in expired] == [request_id]


def test_an_approved_row_nobody_claimed_is_a_queue_not_breakage(client, settings):
    """It used to be a stuck condition, behind a 900 s claim window.

    The window is gone and so is the condition: an approved row is unclaimed
    from the moment it is approved until a claim takes it, which `v_pending`
    says straight away. Nothing here needs to age first, and nothing here is
    broken -- which is why the dashboard shows it under "Approval" rather than
    counting it among the faults.
    """
    request_id = _approved(client, settings)

    stuck = _run(_call(settings.db_path, "view", "v_stuck"))
    assert [r for r in stuck if r["condition"] == "approved_unclaimed"] == []

    pending = _run(_call(settings.db_path, "view", "v_pending"))
    assert [r["id"] for r in pending if r["approved_unclaimed"]] == [request_id]

    # Taking the claim moves it out of `approved` entirely, so it stops being
    # "waiting on me" -- the queue drains by being worked, not by ageing out.
    _run(_call(settings.db_path, "claim", request_id, "owner-a"))
    pending = _run(_call(settings.db_path, "view", "v_pending"))
    assert [r["id"] for r in pending] == []


def test_a_row_with_no_code_ref_cannot_be_claimed(client, settings):
    """§4.1's rule, at the moment it has consequences."""
    seed(
        settings.db_path,
        [
            {
                "id": "req_old",
                "state": "approved",
                "job_json": "{}",
                "code_ref": None,
            }
        ],
    )
    with pytest.raises(ClaimRefused) as caught:
        _run(_call(settings.db_path, "claim", "req_old", "owner-a"))
    assert caught.value.reason == "code_ref_unresolved"


def test_a_code_ref_the_checkout_rejects_cannot_be_claimed(client, settings):
    seed(
        settings.db_path,
        [
            {
                "id": "req_fake",
                "state": "approved",
                "job_json": "{}",
                "code_ref": "0" * 40,
            }
        ],
    )
    # An explicit checker, because "is this sha in the checkout" is a question
    # CI cannot answer -- there is no `.git` there, so the default degrades to
    # *cannot tell* and would let this through.
    with pytest.raises(ClaimRefused) as caught:
        _run(
            _call(
                settings.db_path,
                "claim",
                "req_fake",
                "owner-a",
                code_ref_checker=lambda sha: False,
            )
        )
    assert caught.value.reason == "code_ref_unknown"
    assert caught.value.code_ref == "0" * 40


def test_without_a_checkout_the_check_degrades_to_cannot_tell(client, settings):
    """The image case: the sha is the build arg, so no check is possible.

    `None` means *cannot tell* and must not be read as *not committed* -- the
    alternative is a deployment that refuses to run because it cannot see a
    `.git` directory it was never meant to have.
    """
    seed(
        settings.db_path,
        [
            {
                "id": "req_image",
                "state": "approved",
                "job_json": "{}",
                "code_ref": "a" * 40,
            }
        ],
    )
    row = _run(
        _call(
            settings.db_path,
            "claim",
            "req_image",
            "owner-a",
            code_ref_checker=lambda sha: None,
        )
    )
    assert row["state"] == "running"


def test_the_claim_cli_speaks_the_same_refusals(client, settings):
    def claim(request_id: str, owner: str, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "data_download.claim",
                request_id,
                "--owner",
                owner,
                "--db",
                str(settings.db_path),
                *extra,
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    request_id = _approved(client, settings)
    first = claim(request_id, "owner-a")
    assert first.returncode == 0, first.stderr
    assert "claimed" in first.stdout

    second = claim(request_id, "owner-b")
    assert second.returncode == 1
    assert "claim_refused: leased" in second.stderr

    missing = claim("req_nope", "owner-a")
    assert missing.returncode == 2
    assert "not_found" in missing.stderr


def test_coderef_answers_from_the_checkout_when_there_is_one(tmp_path: Path):
    """Both environments this suite runs in: a checkout, and a CI ticket.

    The suite is executed from a git checkout in the dev container and from a
    ticket on the CI host, so this test may not assume either. What must hold
    everywhere: with a checkout, HEAD is a commit and a made-up sha is not;
    without one, the honest answer is *cannot tell*.
    """
    if not coderef.has_checkout():
        assert coderef.resolve(env={}) is None
        assert coderef.is_commit("0" * 40) is None
        return
    head = coderef.resolve(env={})
    assert head is not None
    assert coderef.is_commit(head) is True
    assert coderef.is_commit("0" * 40) is False
    # A directory with no `.git` cannot answer, whatever it is asked.
    assert coderef.is_commit(head, repo_root=tmp_path) is None


def test_resolve_answers_none_when_there_is_no_checkout_and_no_env(tmp_path: Path):
    """The CI shape: the suite runs from a ticket, so nothing can name a commit.

    `None` here is *cannot tell*, and it becomes `code_ref NULL` at submit and a
    refused claim -- not a crash, and not a made-up sha.
    """
    assert coderef.resolve(env={}, repo_root=tmp_path) is None
    assert coderef.has_checkout(tmp_path) is False


def test_a_git_directory_git_cannot_use_is_not_a_checkout(tmp_path: Path):
    """Build 47: the CI host had a `.git` it was not allowed to read.

    A filesystem check said "yes, a checkout" and every git command said no, so
    a real commit read as *not a commit* and every claim was refused. A checkout
    that cannot be read is no checkout, and the honest answer is *cannot tell*.
    """
    (tmp_path / ".git").mkdir()  # a `.git` that is not a repository
    assert coderef.has_checkout(tmp_path) is False
    assert coderef.is_commit("0" * 40, repo_root=tmp_path) is None
    assert coderef.resolve(env={}, repo_root=tmp_path) is None


def test_coderef_prefers_the_environment_over_the_checkout():
    """The image has no `.git`; its sha is the build arg."""
    assert coderef.resolve(env={coderef.ENV_VAR: "b" * 40}) == "b" * 40
