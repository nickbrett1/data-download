"""The driver queue (phase 5.5): one job per vendor, until a human is asked.

The queue exists because "no driver is installed" is not a failed request. These
tests pin the three properties the loop leans on: the dedupe is the schema's (a
partial unique index), a job becomes *closed* only when it is installed or a
human has been asked, and the request's `driver_state` is a column with an event
rather than a state that would lie about the request.
"""

from __future__ import annotations

import asyncio

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
