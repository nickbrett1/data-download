"""The store's shutdown must not wait forever for a connection thread.

`aiosqlite` reports every answer to the awaiting future's loop from a worker
thread. A cancelled caller whose loop closes first makes that report raise
inside the thread and kills it -- after which `close()` waits on a future
nothing will answer. Observed live: a CI job hung 35 minutes in one thread with
no timers, and the container it was testing would not have stopped either.
"""

from __future__ import annotations

import asyncio
import time

from data_download.db import Store


def test_a_connection_that_will_not_close_does_not_hang_shutdown(tmp_path, monkeypatch):
    class Deaf:
        """A connection whose worker thread is gone: `close` never answers."""

        async def close(self) -> None:
            await asyncio.Event().wait()

    async def go():
        store = Store(str(tmp_path / "store.db"))
        await store.connect()
        store._conn = Deaf()  # type: ignore[assignment]
        monkeypatch.setattr(Store, "close_timeout", 0.05)
        started = time.monotonic()
        await store.close()
        return time.monotonic() - started

    assert asyncio.run(go()) < 2.0
