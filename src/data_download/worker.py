"""The worker: the loop that picks an approved row up without anybody typing a command (§8.1).

Phase 5 built a runner that could run *one named* request. That is a command, not
a service: a person approves a request on a page and then the row sits in
`approved` until somebody notices and runs `python -m data_download.runner`.
This module is the missing "somebody": a pass over the approved queue, on the
event loop this process already has, in the process that already owns the store
and the registry.

Why in this process rather than its own container:

* **The queue is a query, not a broker.** `approved` rows are in sqlite, which
  this process already has open. A second container would want the same file
  (two sqlite writers, one more mount to get wrong) to learn something it could
  ask for in one SELECT.
* **The claim is still the permission.** Two processes -- this loop and a human
  running the CLI -- can both want one row, and `Store.claim`'s CAS decides. The
  worker gets no privilege from being the worker.
* **A crash is already survivable.** The lease is the recovery mechanism, not the
  worker: rows it claimed and did not settle are reclaimable when the lease runs
  out, whether the process died or the container was replaced.

Two things the loop is careful about:

* **It never sleeps while there is work.** A pass that ran something goes
  straight back for the next row, so a backlog drains at the speed of the
  downloads rather than one row per `runner_interval`.
* **It is nudged, not polled, for latency.** The approve route sets an event
  (`Worker.wake`), so a request approved on the page is picked up in
  milliseconds; the interval is only the idle beat that catches rows approved by
  something that could not nudge (a CLI approval, another instance).

A pass is one row at a time, deliberately: two concurrent downloads of the same
dataset compete for bandwidth and disk, and the ceiling check was written for
one attempt at a time. If that changes, it changes here, once.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .db import ClaimRefused, Store
from .registry import Registry
from .runner import RunRefused, RunResult, run_one

log = logging.getLogger(__name__)

#: How many approved rows one pass will take before it lets the loop breathe.
#: A bound rather than a `while True`: a queue that keeps growing (a bug filing
#: in a loop, a vendor that fails instantly) must not stop the process serving
#: the page or noticing a cancel.
PASS_LIMIT = 10


async def tick(
    store: Store,
    *,
    datasets_dir: Path | str | None,
    registry: Registry | None = None,
    driver_url: str | None = None,
    driver_token: str | None = None,
    owner: str | None = None,
    mode: str = "clean",
    lease_seconds: int | None = None,
    driver_factory: Any | None = None,
    limit: int = PASS_LIMIT,
    now: str | None = None,
) -> list[RunResult]:
    """One pass: run the oldest approved rows, oldest first.

    Oldest first because this is a queue, not a stack.

    A refusal on one row is not a refusal of the pass: another process may have
    claimed it (that is the CAS working), or its plan type may have lost its
    vendor. Both are logged and the pass moves on, because the alternative --
    stopping the queue on one bad row -- is how a queue becomes a dead letter
    box. `run_one` settles its own failures, so a `failed` result is still a
    finished row and the pass continues.
    """
    # Asked for in queue order, not sorted here: the page's listing is
    # newest-first and applies its limit first, so sorting afterwards would run
    # the oldest of the newest N (`Store.approved_ids` says why).
    request_ids = await store.approved_ids(limit=limit)
    if not request_ids:
        return []

    results: list[RunResult] = []
    for request_id in request_ids:
        try:
            result = await run_one(
                request_id,
                store=store,
                datasets_dir=datasets_dir,
                owner=owner,
                mode=mode,
                lease_seconds=lease_seconds,
                driver_factory=driver_factory,
                driver_url=driver_url,
                driver_token=driver_token,
                registry=registry,
                now=now,
            )
        except ClaimRefused as exc:
            # Somebody else has it, or it moved while we were looking. Not an
            # error: the approval is still somebody's, just not ours to run.
            log.info("worker: %s not claimed (%s)", request_id, exc.as_text())
            continue
        except RunRefused as exc:
            # Nothing could be run at all (no datasets directory, no token).
            # Everything after this row would be refused the same way, so the
            # pass stops rather than logging the same sentence limit times.
            log.warning("worker: cannot run %s (%s) - stopping this pass", request_id, exc)
            break
        except Exception as exc:  # noqa: BLE001 - one row must not end the loop
            # `run_one` releases the lease on its way out, so the row goes back
            # to `approved` and the next pass tries again. Logged with the
            # traceback because a repeat is a bug, not a vendor outcome.
            log.exception("worker: %s raised", request_id)
            log.warning("worker: %s will be retried after the lease/release", exc)
            continue
        results.append(result)
        log.info(
            "worker: %s -> %s (%s, %d file(s), %d bytes)",
            result.request_id,
            result.state,
            result.verdict or "no verdict",
            len(result.files),
            result.total_bytes,
        )
    return results


class Worker:
    """The loop, and the handle the approve route nudges.

    Started from the app's lifespan so it shares the store, the registry and the
    settings that were already resolved. Nothing here opens a connection:
    a second store would be a second writer to one sqlite file.
    """

    def __init__(
        self,
        store: Store,
        *,
        datasets_dir: Path | str | None,
        registry: Registry | None = None,
        driver_url: str | None = None,
        driver_token: str | None = None,
        interval: float = 60.0,
        lease_seconds: int | None = None,
        mode: str = "clean",
        driver_factory: Any | None = None,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.datasets_dir = datasets_dir
        self.registry = registry
        self.driver_url = driver_url
        self.driver_token = driver_token
        self.interval = interval
        self.lease_seconds = lease_seconds
        self.mode = mode
        self.driver_factory = driver_factory
        self.owner = owner
        #: Set by the approve route. One event, not a queue: a nudge means
        #: "look at the queue again", and looking twice when one new row
        #: arrived costs one SELECT.
        self._nudge = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # -- control -------------------------------------------------------------

    def wake(self) -> None:
        """Somebody approved something (or thinks they did). Never raises."""
        self._nudge.set()

    async def once(self) -> list[RunResult]:
        """One pass, with the exceptions a pass can throw kept out of the loop."""
        try:
            return await tick(
                self.store,
                datasets_dir=self.datasets_dir,
                registry=self.registry,
                driver_url=self.driver_url,
                driver_token=self.driver_token,
                owner=self.owner,
                mode=self.mode,
                lease_seconds=self.lease_seconds,
                driver_factory=self.driver_factory,
            )
        except Exception:  # noqa: BLE001 - the loop outlives a bad pass
            # Whatever it was -- the store went away, a query broke -- the
            # worker's job is to be there for the next row. The traceback is the
            # operator's evidence; stopping the process is not.
            log.exception("worker: pass failed")

            return []

    async def run_forever(self) -> None:
        """Pass, then wait for a nudge or the idle interval. Until cancelled."""
        log.info(
            "worker: watching for approved requests (idle interval %ss)", self.interval
        )
        while not self._stopping.is_set():
            ran = await self.once()
            if ran:
                # Still work to do: no sleep, no lost minute per row.
                continue
            self._nudge.clear()
            try:
                await asyncio.wait_for(self._nudge.wait(), timeout=self.interval)
            except TimeoutError:
                pass
        log.info("worker: stopped")

    def start(self) -> None:
        """Start the loop on the running loop. Idempotent, for a reloaded app."""
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self.run_forever(), name="data-download-worker")

    async def stop(self) -> None:
        """Ask the loop to finish, and be gone before the store is closed.

        Cancelled rather than awaited politely: a pass can be inside a vendor
        download, and the shutdown budget is not hours. The row it was holding
        is `running` with a lease, so a cancelled pass is exactly the case the
        lease exists for -- the next process reclaims it.
        """
        self._stopping.set()
        self._nudge.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.exception("worker: failed on the way down")
