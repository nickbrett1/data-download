"""The driver queue (phase 5.5): one job per vendor, until a human is asked.

The queue exists because "no driver is installed" is not a failed request. These
tests pin the three properties the loop leans on: the dedupe is the schema's (a
partial unique index), a job becomes *closed* only when it is installed or a
human has been asked, and the request's `driver_state` is a column with an event
rather than a state that would lie about the request.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from conftest import seed
from data_download.db import Store


def _run(coro):
    return asyncio.run(coro)


def _store(tmp_path) -> Store:
    store = Store(str(tmp_path / "queue.db"))
    _run(store.connect())
    return store


def test_enqueue_is_one_open_job_per_vendor(tmp_path):
    store = _store(tmp_path)
    try:
        first = _run(store.driver_job_enqueue(vendor="Databento", dataset="GLBX.MDP3"))
        second = _run(store.driver_job_enqueue(vendor="Databento", dataset="EQUS.MINI"))
        other = _run(store.driver_job_enqueue(vendor="Other"))
        assert first is not None and second is None
        assert other is not None
        assert len(_run(store.driver_job_list())) == 2
        assert _run(store.driver_jobs_open()) == 2
    finally:
        _run(store.close())


def test_a_closed_job_allows_a_new_one(tmp_path):
    store = _store(tmp_path)
    try:
        job = _run(store.driver_job_enqueue(vendor="Databento"))
        _run(store.driver_job_update(job["id"], state="installed"))
        assert _run(store.driver_jobs_open()) == 0
        again = _run(store.driver_job_enqueue(vendor="Databento"))
        assert again is not None
        # `needs_human` closes it too: a stop is a stop, not a retry loop.
        _run(store.driver_job_update(again["id"], state="needs_human"))
        assert _run(store.driver_job_enqueue(vendor="Databento")) is not None
    finally:
        _run(store.close())


def test_next_is_the_oldest_queued_and_updates_are_column_checked(tmp_path):
    store = _store(tmp_path)
    try:
        job = _run(
            store.driver_job_enqueue(
                vendor="Databento", now="2026-11-24T01:00:00Z"
            )
        )
        assert (_run(store.driver_job_next()) or {})["id"] == job["id"]
        moved = _run(store.driver_job_update(job["id"], state="building", attempts=1))
        assert moved["state"] == "building"
        assert moved["attempts"] == 1
        assert _run(store.driver_job_next()) is None
        # A job waiting on CI is still work -- but queued work goes first, so a
        # slow pipeline cannot starve a vendor that has not been authored yet.
        waiting = _run(store.driver_job_update(job["id"], state="gated"))
        assert (_run(store.driver_job_next()) or {})["id"] == waiting["id"]
        fresh = _run(
            store.driver_job_enqueue(vendor="Stub", now="2026-11-24T02:00:00Z")
        )
        assert (_run(store.driver_job_next()) or {})["id"] == fresh["id"]
        try:
            _run(store.driver_job_update(job["id"], state="building", wrong="x"))
        except ValueError as exc:
            assert "wrong" in str(exc)
        else:  # pragma: no cover - the raise is the assertion
            raise AssertionError("an unknown column was written")
    finally:
        _run(store.close())


def test_a_job_row_carries_the_waiting_requests_bound_job(db_path):
    """The probe runs the *request's* Job, and `driver_jobs` does not hold it.

    The first acceptance run died at `probing` with "no bound Job on the waiting
    request" for exactly this reason: `job_json` lives on `requests`, the job row
    carries only the request's id, and nothing joined them. The join is in the
    read, so every caller of `driver_job_get`/`driver_job_next` gets it.
    """
    store = Store(str(db_path))
    _run(store.connect())
    try:
        bound = json.dumps({"steps": [{"kind": "quote", "kwargs": {}}]}, sort_keys=True)
        seed(db_path, [{"id": "req_waiting", "state": "approved", "job_json": bound}])
        job = _run(
            store.driver_job_enqueue(vendor="Databento", request_id="req_waiting")
        )
        row = _run(store.driver_job_next())
        assert row["id"] == job["id"]
        assert row["request_job_json"] == bound
        assert row["request_state"] == "approved"
        # A job with no request (a vendor nothing is waiting on) is not an error.
        _run(store.driver_job_update(job["id"], request_id=None))
        assert (_run(store.driver_job_next()) or {})["request_job_json"] is None
    finally:
        _run(store.close())


def test_the_datasets_page_counts_open_driver_jobs(client, settings):
    """The page polls while `in_flight > 0`; a waiting row is not in flight.

    The count rides on the datasets view-model because that is the page that
    keeps looking, and every row that *would* need it is already there.
    """
    body = client.get("/ui/datasets.json").json()
    assert body["totals"]["driver_jobs_open"] == 0

    store = Store(str(settings.db_path))
    _run(store.connect())
    try:
        _run(store.driver_job_enqueue(vendor="Databento", dataset="GLBX.MDP3"))
        _run(store.close())
        body = client.get("/ui/datasets.json").json()
        assert body["totals"]["driver_jobs_open"] == 1
    finally:
        _run(store.close())


def test_a_driver_state_that_did_not_change_writes_no_second_event(tmp_path):
    """The loop asks once a pass; the trail must not answer once a pass.

    A request waiting on a driver that stopped for a human had `note_driver_state`
    called on every pass -- so the page drew twenty identical `driver_state`
    lines, one a minute, and the moment the state actually moved was buried
    among them. The store's own rule for a repeated approval ("one approval is
    one row or the audit trail is not an audit trail") applies here: an
    unchanged state writes nothing, and does not move `updated_at` either,
    which is the row's last real change rather than the last time it was looked
    at.
    """
    store = _store(tmp_path)
    try:
        seed(tmp_path / "queue.db", [{"id": "req_waiting", "state": "approved"}])
        detail = {"vendor": "Databento", "error": "the agent never pushed"}

        _run(store.note_driver_state("req_waiting", "needs_human", detail=detail))
        first = _run(store.events_for("req_waiting"))
        stamped = _run(store.get_request("req_waiting"))["updated_at"]

        # Three more passes over the same row, same answer, later clock.
        for _ in range(3):
            _run(
                store.note_driver_state(
                    "req_waiting", "needs_human", detail=detail, now="2026-11-25T23:59:59Z"
                )
            )

        assert len(_run(store.events_for("req_waiting"))) == 1
        assert len(first) == 1
        assert _run(store.get_request("req_waiting"))["updated_at"] == stamped

        # A *different* reason is news, even at the same state: the stop moved.
        _run(
            store.note_driver_state(
                "req_waiting", "needs_human", detail={"vendor": "Databento", "error": "a bill"}
            )
        )
        events = _run(store.events_for("req_waiting"))
        assert len(events) == 2
        assert json.loads(events[-1]["detail_json"])["error"] == "a bill"
    finally:
        _run(store.close())


def test_a_stopped_build_can_be_retired_and_the_vendor_tried_again(tmp_path):
    """`needs_human` is a stop, and a stop with no way out strands its vendor.

    The runner refuses to queue a new build while the newest row for that vendor
    is stopped -- right, because a job that stopped once stops again the same
    way. Retiring is the deployment-fault exit: the row keeps its error and its
    branch, and a new job may be queued after it.
    """
    store = _store(tmp_path)
    try:
        job = _run(store.driver_job_enqueue(vendor="Databento"))
        _run(store.driver_job_update(job["id"], state="needs_human", last_error="docker missing"))
        retired = _run(store.driver_job_retire(job["id"], note="the key file is mounted now"))
        assert retired["retired_at"]
        assert "docker missing" in retired["last_error"]
        assert "the key file is mounted now" in retired["last_error"]
        # History, not a live answer: the next request may queue a build.
        assert _run(store.driver_job_latest("Databento")) is None
        assert _run(store.driver_job_enqueue(vendor="Databento")) is not None
    finally:
        _run(store.close())


def test_only_a_stopped_build_may_be_retired(tmp_path):
    """A queued or building job is somebody's work in flight."""
    from data_download.db import DriverJobRetireRefused

    store = _store(tmp_path)
    try:
        job = _run(store.driver_job_enqueue(vendor="Databento"))
        with pytest.raises(DriverJobRetireRefused) as refused:
            _run(store.driver_job_retire(job["id"]))
        assert refused.value.reason == "not_stopped"
        assert refused.value.state == "queued"
        # Twice is not a second retirement: the column is already set.
        _run(store.driver_job_update(job["id"], state="needs_human"))
        _run(store.driver_job_retire(job["id"]))
        with pytest.raises(DriverJobRetireRefused):
            _run(store.driver_job_retire(job["id"]))
        with pytest.raises(DriverJobRetireRefused) as missing:
            _run(store.driver_job_retire("drv_nope"))
        assert missing.value.reason == "not_found"
    finally:
        _run(store.close())
