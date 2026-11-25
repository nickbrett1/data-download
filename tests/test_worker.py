"""The worker: the loop that picks an approved row up (§8.1).

Phase 5 could run a request somebody named. What is asserted here is the thing
that was missing: an approval is enough. No command, no `--next`, no operator
watching — a row that reaches `approved` reaches `succeeded` on its own, and a
row that cannot be run stays exactly where it is instead of looking like a
failure.

Four rules, each with a test that would fail if it were dropped:

* **A pass is the queue, oldest first.** Newest-first would let a burst of
  filings starve the request that has been waiting longest.
* **The claim is still the permission.** A row another process holds is skipped,
  not failed, and not stolen.
* **A refusal is not a run.** With nowhere to land bytes the pass stops and the
  row stays `approved`; the alternative is a `failed` row that reads like the
  vendor said no.
* **Losing the lease stops the spending.** `Store.renew` answers false to anyone
  who is not the holder, and the runner records that it happened.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import CODE_REF, PLAN_BODY, REPO_ROOT, TOKENS, auth, seed, served
from data_download.app import create_app
from data_download.config import Settings, parse_interval, parse_switch
from data_download.db import ClaimRefused, Store, shift_seconds, utcnow
from data_download.worker import Worker, made_progress, tick
from fake_runtime import FakeRuntime


@pytest.fixture
def wire(tmp_path):
    """The driver runtime, standing in for the container, as in `test_runner`."""
    with FakeRuntime(tmp_path / "datasets", vendor="Databento", driver="databento") as runtime:
        yield runtime


def _run(coro):
    return asyncio.run(coro)


async def _call(path: Path, method: str, *args, **kwargs):
    store = Store(str(path))
    await store.connect()
    try:
        return await getattr(store, method)(*args, **kwargs)
    finally:
        await store.close()


def _approved(client, settings, body=None) -> str:
    response = client.post("/v1/requests", json=body or PLAN_BODY, headers=auth())
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    _run(_call(settings.db_path, "approve", request_id, "nick"))
    return request_id


def _state(path: Path, request_id: str) -> dict:
    return _run(_call(path, "get_request", request_id))


# -- the pass -------------------------------------------------------------


def test_a_pass_runs_the_approved_row_and_settles_it(
    client, settings, tmp_path, wire
):
    request_id = _approved(client, settings)

    store = Store(settings.db_path)
    _run(store.connect())

    async def pass_once():
        try:
            return await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
        finally:
            await store.close()

    results = _run(pass_once())

    assert [r.request_id for r in results] == [request_id]
    assert results[0].state == "succeeded"
    assert results[0].settled is True

    row = _state(settings.db_path, request_id)
    assert row["state"] == "succeeded"
    assert row["lease_until"] is None
    landed = tmp_path / "datasets" / "databento" / "GLBX.MDP3" / request_id / "files"
    assert len(list(landed.iterdir())) == 2


def test_a_pass_with_nothing_approved_does_nothing(client, settings, tmp_path, wire):
    # Filed, not approved: the queue is `approved`, and a draft is a person's
    # unfinished thought, not work.
    response = client.post("/v1/requests", json=PLAN_BODY, headers=auth())
    assert response.status_code == 202
    request_id = response.json()["request_id"]

    store = Store(settings.db_path)
    _run(store.connect())

    async def pass_once():
        try:
            return await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
        finally:
            await store.close()

    assert _run(pass_once()) == []
    assert _state(settings.db_path, request_id)["state"] == "draft"


def test_the_oldest_approved_row_goes_first(client, settings, tmp_path, wire):
    first = _approved(client, settings)
    second = _approved(client, settings)
    # Both are created in the same second in real life, which is exactly why the
    # order has to be pinned here: the queue's claim is "oldest first", and a tie
    # must not turn that into "whichever the list happened to return".
    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute(
            "UPDATE requests SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", first),
        )
        conn.execute(
            "UPDATE requests SET created_at = ? WHERE id = ?",
            ("2026-02-01T00:00:00Z", second),
        )
        conn.commit()
    finally:
        conn.close()

    store = Store(settings.db_path)
    _run(store.connect())

    async def first_pass():
        try:
            return await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
                limit=1,
            )
        finally:
            await store.close()

    results = _run(first_pass())
    assert [r.request_id for r in results] == [first]
    assert _state(settings.db_path, second)["state"] == "approved"


def test_a_row_another_claimant_holds_is_skipped_not_failed(
    client, settings, tmp_path, wire
):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "other-process"))

    store = Store(settings.db_path)
    _run(store.connect())

    async def pass_once():
        try:
            return await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
        finally:
            await store.close()

    assert _run(pass_once()) == []
    row = _state(settings.db_path, request_id)
    # Still the other claimant's: the CAS was the decision, and losing it is
    # not ours to undo.
    assert row["state"] == "running"
    assert row["owner"] == "other-process"


def test_a_pass_that_cannot_land_anything_leaves_the_row_approved(
    client, settings, wire
):
    """No datasets directory: refuse before the claim, so the row is still a
    candidate the moment somebody fixes the setting."""
    request_id = _approved(client, settings)

    store = Store(settings.db_path)
    _run(store.connect())

    async def pass_once():
        try:
            return await tick(
                store,
                datasets_dir=None,
                driver_url=wire.url,
                driver_token=wire.token,
            )
        finally:
            await store.close()

    assert _run(pass_once()) == []
    row = _state(settings.db_path, request_id)
    assert row["state"] == "approved"
    assert row["owner"] is None


def test_a_row_waiting_for_a_driver_is_queued_and_left_where_it_is(
    client, settings, wire, tmp_path
):
    """No driver for the vendor: a job for the authoring loop, not an attempt.

    The row stays `approved` so it runs the moment the driver lands, and
    `run_one` says `settled=False` to make that legible -- the pass did not
    settle anything, which is what the loop reads to decide whether to spin.
    """
    wire.drivers = {}  # the runtime is up, and has nothing for Databento
    request_id = _approved(client, settings)

    store = Store(settings.db_path)
    _run(store.connect())

    async def pass_once():
        try:
            return await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
        finally:
            await store.close()

    results = _run(pass_once())
    assert [r.refused for r in results] == ["driver_missing"]
    assert results[0].settled is False
    assert made_progress(results) is False
    row = _state(settings.db_path, request_id)
    assert row["state"] == "approved"
    assert row["owner"] is None
    assert _run(_call(settings.db_path, "driver_jobs_open")) == 1


def test_a_job_that_stopped_for_a_human_is_not_queued_a_second_time(
    client, settings, wire, tmp_path
):
    """`needs_human` is a stop, so the next pass must not mint another job.

    The open-jobs index excludes `needs_human` on purpose (a *new* request may
    file a new job), which means the index alone allowed one fresh job per pass
    for a request that keeps being re-approved. On the deployment that was a
    job a minute, each dying before any spend -- but a driver that is merely
    wrong fails *after* the authoring conversation, and a job a minute is then
    a bill rather than a retry.
    """
    wire.drivers = {}
    request_id = _approved(client, settings)

    store = Store(settings.db_path)
    _run(store.connect())

    async def go():
        try:
            first = await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
            job = await store.driver_job_latest("Databento")
            await store.driver_job_update(
                job["id"], state="needs_human", last_error="the agent never pushed"
            )
            second = await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
            # A third pass, because the loop asks once a minute and the page
            # must not gain a line a minute: the state did not move.
            third = await tick(
                store,
                datasets_dir=tmp_path / "datasets",
                driver_url=wire.url,
                driver_token=wire.token,
            )
            return first, second, third, await store.driver_job_list(), await store.events_for(
                request_id
            )
        finally:
            await store.close()

    first, second, third, jobs, events = _run(go())
    assert [r.refused for r in first] == ["driver_missing"]
    assert len(jobs) == 1, "a stopped job must not be replaced by a new one"
    assert second[0].settled is False
    assert "stopped for a human" in second[0].errors["detail"]
    assert "the agent never pushed" in second[0].errors["detail"]
    assert _state(settings.db_path, request_id)["driver_state"] == "needs_human"
    # One line for the stop, not one per pass. `building` then `needs_human`;
    # the third pass repeats the second and writes nothing.
    states = [
        e for e in events if e["event"] == "driver_state"
    ]
    assert [json.loads(e["detail_json"])["driver_state"] for e in states] == [
        "building",
        "needs_human",
    ], "a pass that finds the same stop has nothing to add"
    assert third[0].settled is False


def test_a_row_waiting_for_a_driver_does_not_spin_the_loop(
    client, settings, wire, tmp_path
):
    """The loop sleeps while a driver is being written (§8.1's idle beat).

    With the row left `approved` and a result that settles nothing, a loop that
    treats "the pass returned something" as work re-runs the same row at full
    speed -- measured at ~640 passes a second on the deployed container, for as
    long as authoring takes. The idle interval is what it must fall back to.
    """
    wire.drivers = {}
    _approved(client, settings)
    driver_reads = lambda: sum(  # noqa: E731 - a counter, not a function
        1 for method, path in wire.requests if (method, path) == ("GET", "/v1/drivers")
    )

    store = Store(settings.db_path)
    _run(store.connect())

    async def go():
        worker = Worker(
            store,
            datasets_dir=tmp_path / "datasets",
            driver_url=wire.url,
            driver_token=wire.token,
            interval=30.0,
        )
        worker.start()
        await asyncio.sleep(0.5)
        await worker.stop()
        return driver_reads()

    reads = _run(go())
    # One pass, and then the loop waits on its nudge/timeout rather than
    # asking again. Anything near the pass rate of a spin would be hundreds.
    assert reads <= 2, f"the loop asked {reads} times in half a second"


# -- the lease ------------------------------------------------------------


def test_renew_extends_only_the_holders_lease(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "mine", lease_seconds=60))
    before = _state(settings.db_path, request_id)["lease_until"]

    assert _run(_call(settings.db_path, "renew", request_id, "mine", lease_seconds=3600))
    after = _state(settings.db_path, request_id)["lease_until"]
    assert after > before
    assert after == shift_seconds(utcnow(), 3600)

    # Anybody else is not the holder, and a heartbeat from them must not move
    # the clock -- that is the reclaim working.
    assert not _run(_call(settings.db_path, "renew", request_id, "somebody-else"))
    assert _state(settings.db_path, request_id)["lease_until"] == after


def test_renew_refuses_once_the_row_has_settled(client, settings):
    request_id = _approved(client, settings)
    _run(_call(settings.db_path, "claim", request_id, "mine"))
    _run(_call(settings.db_path, "settle", request_id, "mine", "failed"))

    assert not _run(_call(settings.db_path, "renew", request_id, "mine"))


def test_a_heartbeat_keeps_a_run_that_outlives_the_lease(tmp_path):
    """The §5.2 defect, as a test: a lease of 0.4 s and a run of 1.2 s.

    Without the heartbeat the row is reclaimable while the first attempt is
    still inside the vendor call, and the vendor is asked twice. With it, the
    second claimant is refused for as long as the first is alive.
    """
    from data_download.runner import keep_lease

    db = tmp_path / "lease.db"

    async def scenario():
        store = Store(str(db))
        await store.connect()
        try:
            # A row in `running`, as `claim` would leave it: a lease of one
            # second, and a run that will outlive it.
            seed(
                db,
                [
                    {
                        "id": "req_beat",
                        "state": "running",
                        "owner": "mine",
                        "lease_until": shift_seconds(utcnow(), 1),
                    }
                ],
            )

            stop, lost = asyncio.Event(), asyncio.Event()
            beat = asyncio.create_task(
                keep_lease(
                    store, "req_beat", "mine", lease_seconds=1, stop=stop, lost=lost
                )
            )
            await asyncio.sleep(2.4)  # two and a bit beats
            still_mine = not lost.is_set()
            # A stranger cannot claim it while the beat is alive.
            refused = None
            try:
                await store.claim("req_beat", "thief", lease_seconds=1)
            except ClaimRefused as exc:
                refused = exc.reason
            stop.set()
            await beat
            return still_mine, refused
        finally:
            await store.close()

    still_mine, refused = _run(scenario())
    assert still_mine is True
    assert refused == "leased"


def test_a_heartbeat_stops_when_the_lease_is_taken_away(tmp_path):
    from data_download.runner import keep_lease

    async def scenario():
        store = Store(str(tmp_path / "gone.db"))
        await store.connect()
        # A store with tables and no such row: the first beat is the one that
        # says false, and the task ends itself rather than beating on a row that
        # cannot be its own.
        await store.connect()
        try:
            stop, lost = asyncio.Event(), asyncio.Event()
            beat = asyncio.create_task(
                keep_lease(store, "req_nothing", "mine", lease_seconds=1, stop=stop, lost=lost)
            )
            await asyncio.wait_for(lost.wait(), timeout=5)
            await beat
            return lost.is_set()
        finally:
            await store.close()

    assert _run(scenario()) is True


# -- the loop -------------------------------------------------------------


def test_the_worker_is_not_started_when_it_could_not_finish(settings):
    """A loop that can only log a refusal is worse than no loop: it reads like
    an attempt and changes nothing."""
    app = create_app(settings)  # no datasets_dir, no driver token
    with TestClient(app, client=("127.0.0.1", 51234)):
        assert app.state.worker is None


def test_switches_and_intervals_refuse_nonsense():
    assert parse_switch(None) is True  # on by default
    assert parse_switch("off") is False
    assert parse_switch("0") is False
    assert parse_switch("nonsense") is True  # a typo must not silently stop the queue
    assert parse_interval(None) == 60.0
    assert parse_interval("15") == 15.0
    assert parse_interval("0") == 60.0  # a spin loop is not an interval
    assert parse_interval("60000") == 60.0  # reads like ms; would be 16 hours


def _wait_for_state(path: Path, request_id: str, state: str, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(
                    "SELECT state FROM requests WHERE id = ?", (request_id,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            row = None
        last = row[0] if row else last
        if last == state:
            return last
        time.sleep(0.05)
    raise AssertionError(f"{request_id} never reached {state!r} (last: {last!r})")


def test_approving_on_the_page_is_enough(tmp_path, db_path, wire):
    """The acceptance case, through the doors a person uses: file, approve, wait.

    No runner command is run in this test. The app owns the loop; the approve
    route nudges it; the row lands bytes. Its idle interval is an hour, so what
    makes this pass quickly is the nudge -- which is exactly the thing that was
    missing when the page said "no worker has picked it up".
    """
    settings = Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=REPO_ROOT / "plan_types",
        approvers=frozenset({"nick@example.com"}),
        code_ref=CODE_REF,
        quote=lambda kwargs: 0.42,
        datasets_dir=tmp_path / "datasets",
        driver_runtime_url=wire.url,
        driver_runtime_token=wire.token,
        runner=True,
        runner_interval=3600.0,
    )
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 51234)) as test_client:
        assert app.state.worker is not None
        filed = test_client.post("/v1/requests", json=PLAN_BODY, headers=auth())
        assert filed.status_code == 202, filed.text
        request_id = filed.json()["request_id"]

        approved = test_client.post(
            f"/v1/requests/{request_id}/approve", json={}, headers=served()
        )
        assert approved.status_code == 200, approved.text

        assert _wait_for_state(db_path, request_id, "succeeded") == "succeeded"

        detail = test_client.get(f"/ui/request/{request_id}.json").json()
        assert detail["state"] == "succeeded"

    landed = tmp_path / "datasets" / "databento" / "GLBX.MDP3" / request_id / "files"
    assert len(list(landed.iterdir())) == 2


def test_the_loop_stops_with_the_app(tmp_path, db_path, wire):
    """No task outlives the store it writes to."""
    settings = Settings(
        db_path=str(db_path),
        tokens=TOKENS,
        plan_types_dir=REPO_ROOT / "plan_types",
        approvers=frozenset({"nick@example.com"}),
        code_ref=CODE_REF,
        quote=lambda kwargs: 0.42,
        datasets_dir=tmp_path / "datasets",
        driver_runtime_url=wire.url,
        driver_runtime_token=wire.token,
        runner=True,
        runner_interval=0.05,
    )
    app = create_app(settings)
    with TestClient(app, client=("127.0.0.1", 51234)):
        worker = app.state.worker
        assert isinstance(worker, Worker)
        assert worker._task is not None and not worker._task.done()
    assert worker._task is None
