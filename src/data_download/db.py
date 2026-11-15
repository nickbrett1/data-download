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


def job_hash(job_bytes: str) -> str:
    """`job_hash` is derived from the stored bytes, never authored (§4.2).

    Takes the canonical *text*, not the object: hashing exactly what will be
    stored is what makes the hash an attestation rather than a second opinion.
    """
    return hashlib.sha256(job_bytes.encode("utf-8")).hexdigest()


# Columns that arrived after the first deployment. `CREATE TABLE IF NOT EXISTS`
# is a no-op on an existing database, so a new column has to be added by hand
# before the schema (whose views select it) can be applied. Additive and
# nullable only: this is not a migration system, it is the two lines that keep
# a one-row store readable after a schema change.
ADDED_COLUMNS = (
    ("requests", "why", "TEXT"),
    ("requests", "max_usd", "REAL"),
)


async def _add_missing_columns(conn: aiosqlite.Connection) -> None:
    for table, column, decl in ADDED_COLUMNS:
        async with conn.execute(f"PRAGMA table_info({table})") as cur:
            present = {row["name"] for row in await cur.fetchall()}
        if not present:  # a fresh database: `executescript` will create it
            continue
        if column not in present:
            log.info("adding column %s.%s", table, column)
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# The phase-2 rebuild (§5.6). One migration does two things because the deployed
# store holds a single row and after phase 3 it is an executing system's history:
#
#   * `plan_hash`/`plan_json` are renamed `job_hash`/`job_json` -- the artefact is
#     a Job now (§0), and a hash over a document whose name says "plan" is the
#     ambiguity this phase exists to remove;
#   * `payload_hash` stops being UNIQUE, because a re-request is a second Request
#     against the same Plan, not a duplicate to refuse.
#
# SQLite cannot drop a constraint in place, so this is create/copy/drop/rename
# inside one BEGIN IMMEDIATE. It is idempotent: the condition is "the old shape
# is present", so re-running the order on an already-migrated store is a no-op.
REQUESTS_TABLE_DDL = """
CREATE TABLE requests_phase2 (
  id               TEXT PRIMARY KEY,
  state            TEXT NOT NULL,
  body_kind        TEXT NOT NULL,
  plan_type        TEXT,
  job_json         TEXT,
  params_json      TEXT NOT NULL,
  payload_hash     TEXT NOT NULL,
  idempotency_key  TEXT,
  intent_json      TEXT,
  estimate_usd     REAL,
  blocked_reason   TEXT,
  plan_origin      TEXT,
  matched_profile  TEXT,
  code_ref         TEXT,
  job_hash         TEXT,
  lease_until      TEXT,
  landed_sha256    TEXT,
  errors_json      TEXT,
  why              TEXT,
  max_usd          REAL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  CHECK (state IN ('draft','blocked','approved','running','succeeded','landed_with_errors','failed')),
  CHECK (body_kind IN ('plan','intent')),
  CHECK ((state = 'blocked') = (job_json IS NULL)),
  CHECK (idempotency_key IS NULL OR length(idempotency_key) > 0),
  CHECK (state <> 'blocked' OR estimate_usd IS NULL),
  CHECK (blocked_reason IS NULL OR state = 'blocked'),
  CHECK ((body_kind = 'intent') = (intent_json IS NOT NULL)),
  CHECK (blocked_reason IS NULL OR blocked_reason IN
         ('awaiting_plan','plan_rejected','resolver_stalled','ceiling_exceeded'))
)
"""

_PHASE2_COLUMNS = (
    "id, state, body_kind, plan_type, job_json, params_json, payload_hash, "
    "idempotency_key, intent_json, estimate_usd, blocked_reason, plan_origin, "
    "matched_profile, code_ref, job_hash, lease_until, landed_sha256, "
    "errors_json, why, max_usd, created_at, updated_at"
)


def _needs_rebuild(columns: set[str], indexes: list[tuple[str, int, str]]) -> bool:
    """True when the table is still phase 1's shape (§5.6)."""
    if "plan_json" in columns or "plan_hash" in columns:
        return True
    if "job_json" not in columns:
        return True
    # A UNIQUE index over exactly (payload_hash,) with SQLite's table-constraint
    # origin is the constraint this migration drops.
    return any(unique and origin == "u" and name.startswith("sqlite_autoindex")
               for name, unique, origin in indexes)


async def _migrate_requests(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(requests)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    if not columns:  # a fresh database: the schema will create the new shape
        return

    async with conn.execute("PRAGMA index_list(requests)") as cur:
        indexes = [
            (row["name"], int(row["unique"]), row["origin"])
            for row in await cur.fetchall()
        ]
    # Only an autoindex that covers payload_hash means "payload_hash is UNIQUE",
    # but a phase-1 table's only unique autoindex is on payload_hash, and the
    # table-level rename check catches the rest.
    if not _needs_rebuild(columns, indexes):
        return

    log.info("rebuilding requests: plan_json/plan_hash -> job_json/job_hash, "
             "payload_hash loses UNIQUE")
    # DROP TABLE requests with foreign_keys=ON would fire the events FK, and the
    # pragma is a no-op inside a transaction - so it goes first, outside it.
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("DROP TABLE IF EXISTS requests_phase2")
        await conn.execute(REQUESTS_TABLE_DDL)
        copy = []
        for column in _PHASE2_COLUMNS.split(", "):
            if column == "job_json":
                copy.append("plan_json" if "plan_json" in columns else "NULL")
            elif column == "job_hash":
                copy.append("plan_hash" if "plan_hash" in columns else "NULL")
            elif column == "idempotency_key":
                copy.append("idempotency_key" if "idempotency_key" in columns else "NULL")
            elif column in columns:
                copy.append(column)
            else:
                copy.append("NULL")
        await conn.execute(
            f"INSERT INTO requests_phase2 ({_PHASE2_COLUMNS}) "
            f"SELECT {', '.join(copy)} FROM requests"
        )
        await conn.execute("DROP TABLE requests")
        await conn.execute("ALTER TABLE requests_phase2 RENAME TO requests")
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


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
        await _add_missing_columns(conn)
        await _migrate_requests(conn)
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
        """The most recent Request with this body -- an informational lookup."""
        async with self.conn.execute(
            "SELECT * FROM requests WHERE payload_hash = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (payload,),
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

    async def overview_one(self, request_id: str) -> dict[str, Any] | None:
        """One row of the same view the list is built from.

        The detail page has to agree with the list it was reached from: same
        age, same facet, same stuck condition. Recomputing those from the table
        would be a second opinion, and two answers to "why is this row
        interesting" is the drift §7.8 warns about.
        """
        async with self.conn.execute(
            "SELECT * FROM v_requests WHERE id = ?", (request_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    # -- write -------------------------------------------------------------

    async def insert_request(
        self,
        *,
        row: dict[str, Any],
        event_detail: dict[str, Any],
    ) -> tuple[dict[str, Any], bool, str | None]:
        """Insert one request and its single `submitted` event, atomically.

        Returns `(row, created, duplicate_of)`.

        * `duplicate_of` is **informational** (§5.6): the id of an earlier
          Request with the same `payload_hash`. The row is still created -- a
          re-request is a Request, with its own id and its own output directory,
          and it is not this service's job to stop a user paying twice.
        * `created=False` happens only when the caller supplied an
          `Idempotency-Key` that has already been used: that is the caller
          saying "this is the same attempt", which is the one thing the service
          cannot work out for itself.
        """
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                key = row.get("idempotency_key")
                if key:
                    async with self.conn.execute(
                        "SELECT * FROM requests WHERE idempotency_key = ?", (key,)
                    ) as cur:
                        existing = await cur.fetchone()
                    if existing is not None:
                        await self.conn.execute("ROLLBACK")
                        return dict(existing), False, str(existing["id"])

                async with self.conn.execute(
                    "SELECT id FROM requests WHERE payload_hash = ? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (row["payload_hash"],),
                ) as cur:
                    earlier = await cur.fetchone()
                duplicate_of = str(earlier["id"]) if earlier else None

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
                return row, True, duplicate_of
            except sqlite3.IntegrityError:
                # Lost a race on the UNIQUE idempotency key; the winner's row is
                # the answer, not an error.
                await self.conn.execute("ROLLBACK")
                key = row.get("idempotency_key")
                if not key:
                    raise
                async with self.conn.execute(
                    "SELECT * FROM requests WHERE idempotency_key = ?", (key,)
                ) as cur:
                    existing = await cur.fetchone()
                if existing is None:
                    raise
                return dict(existing), False, str(existing["id"])
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
