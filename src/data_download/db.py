"""SQLite persistence: one connection, WAL, BEGIN IMMEDIATE on every write.

Pragmas are applied per connection rather than once at startup, because
`aiosqlite` hands out connections; there is no single global one to configure.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)


def utcnow() -> str:
    """ISO-8601 UTC, seconds precision, `Z` suffix - sortable and julianday-safe."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, no unicode escaping differences."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(body: Any) -> str:
    """Idempotence key: a hash of the canonicalised submitted body (§7.4)."""
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def plan_hash(plan: Any) -> str:
    """`plan_hash` is derived, never authored (§4.2)."""
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        for pragma in WRITE_PRAGMAS:
            await conn.execute(pragma)
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        await conn.executescript(schema)
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("store is not connected")
        return self._conn

    # -- reads -------------------------------------------------------------

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        async with self.conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def find_by_payload_hash(self, payload: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM requests WHERE payload_hash = ?", (payload,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_requests(
        self,
        *,
        state: str | None = None,
        plan_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        args: list[Any] = []
        if state:
            where.append("state = ?")
            args.append(state)
        if plan_type:
            where.append("plan_type = ?")
            args.append(plan_type)
        clause = f"WHERE {' AND '.join(where)}" if where else ""

        async with self.conn.execute(
            f"SELECT COUNT(*) AS n FROM requests {clause}", args
        ) as cur:
            total = (await cur.fetchone())["n"]

        async with self.conn.execute(
            f"SELECT * FROM requests {clause} ORDER BY created_at DESC, rowid DESC "
            "LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return rows, total

    async def events_for(self, request_id: str) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM events WHERE request_id = ? ORDER BY id ASC", (request_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def view(self, name: str) -> list[dict[str, Any]]:
        if name not in {"v_pending", "v_done", "v_stuck", "v_blocked"}:
            raise ValueError(f"unknown view {name!r}")
        async with self.conn.execute(f"SELECT * FROM {name}") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def overview(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Every request once, newest first, with its attention flag attached.

        The dashboard shows one list rather than four sections; this is that
        list's source. `v_pending`/`v_done`/`v_stuck`/`v_blocked` remain the
        semantic truth (each is still queried and tested on its own), they just
        are not the page's layout any more.
        """
        async with self.conn.execute(
            "SELECT * FROM v_requests ORDER BY created_at DESC, seq DESC LIMIT ?",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -- write -------------------------------------------------------------

    async def insert_request(
        self,
        *,
        row: dict[str, Any],
        event_detail: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Insert one request and its single `submitted` event, atomically.

        Returns `(row, created)`. When `payload_hash` already exists the
        existing row is returned with `created=False` and nothing is written.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                async with self.conn.execute(
                    "SELECT * FROM requests WHERE payload_hash = ?", (row["payload_hash"],)
                ) as cur:
                    existing = await cur.fetchone()
                if existing is not None:
                    await self.conn.execute("ROLLBACK")
                    return dict(existing), False

                columns = ", ".join(row.keys())
                placeholders = ", ".join("?" for _ in row)
                await self.conn.execute(
                    f"INSERT INTO requests ({columns}) VALUES ({placeholders})",
                    list(row.values()),
                )
                await self.conn.execute(
                    "INSERT INTO events (request_id, ts, actor_kind, event, detail_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["id"],
                        row["created_at"],
                        "submitter",
                        "submitted",
                        canonical_json(event_detail),
                    ),
                )
                await self.conn.execute("COMMIT")
                return row, True
            except sqlite3.IntegrityError:
                # Lost a race on the UNIQUE payload_hash; the winner's row is
                # the answer, not an error.
                await self.conn.execute("ROLLBACK")
                async with self.conn.execute(
                    "SELECT * FROM requests WHERE payload_hash = ?", (row["payload_hash"],)
                ) as cur:
                    existing = await cur.fetchone()
                if existing is None:
                    raise
                return dict(existing), False
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise

    # -- long poll ---------------------------------------------------------

    async def wait_for_state_change(
        self, request_id: str, initial_state: str, timeout: float
    ) -> dict[str, Any] | None:
        """Hold a read until `state` changes or `timeout` expires (§7.9)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            row = await self.get_request(request_id)
            if row is None or row["state"] != initial_state:
                return row
            if asyncio.get_event_loop().time() >= deadline:
                return row
            await asyncio.sleep(0.25)
