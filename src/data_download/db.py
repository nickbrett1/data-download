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
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from . import coderef

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


def shift_seconds(iso: str, seconds: int) -> str:
    """`lease_until` = now + the lease, in the same fixed format as `utcnow`."""
    moment = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (moment + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # Phase 3: the claimant (§8.2). Nullable, like the rest of this tuple, and
    # for the same reason -- the store may already hold rows.
    ("requests", "owner", "TEXT"),
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
  owner            TEXT,
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


# The phase-3 rebuild: `events.actor_kind` gains `human` (the CLI approver).
# SQLite cannot widen a CHECK in place, so this is the same create/copy/drop/
# rename the phase-2 rebuild uses. No value is rewritten -- `submitter` still
# means "an API caller", and relabelling every past submit as a human approval-
# adjacent act would be a worse record than the one we have.
EVENTS_TABLE_DDL = """
CREATE TABLE events_phase3 (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id   TEXT NOT NULL REFERENCES requests(id),
  ts           TEXT NOT NULL,
  actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('submitter','human','agent','runner','system')),
  event        TEXT NOT NULL,
  detail_json  TEXT
)
"""


async def _migrate_events(conn: aiosqlite.Connection) -> None:
    async with conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
    ) as cur:
        row = await cur.fetchone()
    if row is None:  # a fresh database: `executescript` will create it
        return
    if "'human'" in (row["sql"] or ""):
        return
    log.info("rebuilding events: actor_kind gains 'human'")
    await conn.execute("PRAGMA foreign_keys=OFF")
    await conn.execute("BEGIN IMMEDIATE")
    try:
        await conn.execute(EVENTS_TABLE_DDL)
        await conn.execute(
            "INSERT INTO events_phase3 (id, request_id, ts, actor_kind, event, detail_json) "
            "SELECT id, request_id, ts, actor_kind, event, detail_json FROM events"
        )
        await conn.execute("DROP TABLE events")
        await conn.execute("ALTER TABLE events_phase3 RENAME TO events")
        await conn.execute("COMMIT")
    except Exception:
        await conn.execute("ROLLBACK")
        raise
    finally:
        await conn.execute("PRAGMA foreign_keys=ON")


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


# The lease a claim takes by default. Policy, not a literal in the statement:
# phase 2 learned this with `MAX_POLLS`, and the same rule applies before it can
# calcify. Overridable per call so the exit test does not need a 15-minute sleep.
LEASE_SECONDS = 900


class ApproveRefused(Exception):
    """`approve` said no, with one of a closed set of reasons (phase 3 §2)."""

    REASONS = frozenset({"not_found", "blocked", "wrong_state", "job_missing"})

    def __init__(self, reason: str, state: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state

    def as_text(self) -> str:
        return f"approve_refused: {self.reason} (state={self.state})"


class ClaimRefused(Exception):
    """The claim lost, and why -- so neither caller has to invent a reason.

    Returning `None` from `claim()` would push the diagnosis onto a CLI and a
    runner that would each write their own, which is how two vocabularies for
    one fact happen.
    """

    REASONS = frozenset(
        {
            "not_found",
            "blocked",
            "draft",
            "settled",
            "leased",
            "code_ref_unresolved",
            "code_ref_unknown",
        }
    )

    def __init__(
        self,
        reason: str,
        state: str | None = None,
        lease_until: str | None = None,
        code_ref: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state
        self.lease_until = lease_until
        self.code_ref = code_ref

    def as_text(self) -> str:
        parts = [f"claim_refused: {self.reason}", f"state={self.state}"]
        if self.lease_until:
            parts.append(f"lease_until={self.lease_until}")
        if self.code_ref:
            parts.append(f"code_ref={self.code_ref}")
        return " ".join(parts)


# The five read-surface views. All of them are queries: dropping one loses
# nothing, which is what makes them safe to rebuild when the schema is confused.
VIEW_NAMES = ("v_pending", "v_done", "v_stuck", "v_blocked", "v_requests")


def _statements(schema: str) -> list[str]:
    """The schema, split into statements so a failure can name the one that broke.

    `executescript` reports "no such table: main.requests" with no indication of
    which statement asked, which cost an hour on build 48. Comments go first --
    this file's prose is full of semicolons -- and only then is the rest split,
    which is exact because no string literal or CHECK in the schema contains one.
    """
    without_comments = "\n".join(
        line.split("--", 1)[0] for line in schema.splitlines()
    )
    return [chunk.strip() for chunk in without_comments.split(";") if chunk.strip()]


async def _apply_schema(conn: aiosqlite.Connection) -> None:
    for statement in _statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        try:
            await conn.execute(statement)
        except sqlite3.Error as exc:
            headline = statement.lstrip("\n-").splitlines()[0][:90]
            raise sqlite3.OperationalError(f"{exc} [in: {headline}]") from exc


async def _heal_views(conn: aiosqlite.Connection) -> None:
    """Drop the views when the table they select is not there.

    A half-finished rebuild, or another process's DDL, can leave the five views
    standing over a missing `requests`. They are queries, so throwing them away
    costs nothing and is the only way the schema can be re-applied cleanly.
    """
    async with conn.execute("PRAGMA table_info(requests)") as cur:
        has_table = bool(await cur.fetchall())
    if has_table:
        return
    async with conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type = 'view' AND name IN "
        "('v_pending','v_done','v_stuck','v_blocked','v_requests')"
    ) as cur:
        row = await cur.fetchone()
    if row and row[0]:
        log.warning("requests is missing but %d read-surface views remain: dropping them", row[0])
        for name in VIEW_NAMES:
            await conn.execute(f"DROP VIEW IF EXISTS {name}")


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the store, retrying a bounded number of times on a fresh connection.

        The retry is not defensiveness for its own sake. A deploy briefly runs
        two containers against one database, and SQLite's schema cache is
        per-connection: DDL from the old process can leave the new one holding a
        view over a table it can no longer see (`error in view v_pending: no such
        table: main.requests` -- how build 48's first boot died, on a file that
        was completely healthy). A stale cache cannot be repaired on the
        connection that has it, so the fix is a new connection, not a retry of
        the statement.
        """
        attempts = 1 if self.path == ":memory:" else 3
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                await self._open()
                return
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last = exc
                log.warning(
                    "opening the store failed (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                    exc_info=True,
                )
                await self._close_quietly()
                if attempt < attempts:
                    await asyncio.sleep(0.2 * attempt)
        assert last is not None
        raise last

    async def _open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            for pragma in WRITE_PRAGMAS:
                await conn.execute(pragma)
            await _add_missing_columns(conn)
            await _migrate_requests(conn)
            await _migrate_events(conn)
            await _heal_views(conn)
            await _apply_schema(conn)
            # A read through the busiest view, on this connection, before the
            # connection is published. If the schema cache is stale, this is
            # where it stops being someone's 503.
            async with conn.execute("SELECT count(*) FROM v_pending") as cur:
                await cur.fetchone()
        except Exception:
            await conn.close()
            raise
        self._conn = conn

    async def _close_quietly(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001 - closing a broken connection
                log.debug("closing a failed store connection", exc_info=True)

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

    # -- phase 3: approval, the claim, the lease (§8.2) --------------------

    async def _select(self, request_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _event(
        self,
        request_id: str,
        ts: str,
        actor_kind: str,
        event: str,
        detail: dict[str, Any],
    ) -> None:
        """Append one event. Always called inside the caller's transaction.

        A claim whose event is missing is an unauditable spend and there is no
        correct way to repair it, so state and event commit together or not at
        all (phase 3 order §3).
        """
        await self.conn.execute(
            "INSERT INTO events (request_id, ts, actor_kind, event, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (request_id, ts, actor_kind, event, canonical_json(detail)),
        )

    async def approve(
        self,
        request_id: str,
        actor: str,
        note: str | None = None,
        *,
        now: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """`draft` -> `approved`. Returns `(row, changed)`.

        Approving an already-approved row is a **no-op with no second event**: a
        retried CLI call must not be able to inflate the record, and one
        approval is one row or the audit trail is not an audit trail.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise ApproveRefused("not_found")
                state = row["state"]
                if state == "approved":
                    await self.conn.execute("ROLLBACK")
                    return row, False
                if state == "blocked":
                    raise ApproveRefused("blocked", state)
                if state != "draft":
                    raise ApproveRefused("wrong_state", state)
                if not row["job_json"]:
                    # The CHECK forbids it; a hand-edited store is not bound by
                    # a CHECK enforced when the row was written.
                    raise ApproveRefused("job_missing", state)
                await self.conn.execute(
                    "UPDATE requests SET state = 'approved', updated_at = ? "
                    "WHERE id = ? AND state = 'draft'",
                    (ts, request_id),
                )
                await self._event(
                    request_id,
                    ts,
                    "human",
                    "approved",
                    {"actor": actor, "note": note, "job_hash": row["job_hash"]},
                )
                await self.conn.execute("COMMIT")
            except ApproveRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return (await self.get_request(request_id) or row), True

    async def claim(
        self,
        request_id: str,
        owner: str,
        *,
        lease_seconds: int | None = None,
        now: str | None = None,
        code_ref_checker: Callable[[str], bool | None] | None = None,
    ) -> dict[str, Any]:
        """Take the lease on an approved (or abandoned) row -- one statement.

        Reclaim is **not a second mechanism**: a claim on an expired lease is the
        same CAS, so there is no window between a sweeper and a claim in which
        two claimants can both believe the row is theirs. There is no sweeper.

        Refusals are typed (`ClaimRefused`), because the callers are a CLI and a
        runner and a bare `None` would make each of them invent a vocabulary for
        the same fact.
        """
        lease = LEASE_SECONDS if lease_seconds is None else lease_seconds
        ts = now or utcnow()
        until = shift_seconds(ts, lease)
        checker: Callable[[str], bool | None] = (
            coderef.is_commit if code_ref_checker is None else code_ref_checker
        )
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise ClaimRefused("not_found")
                state = row["state"]
                expired = (
                    state == "running"
                    and bool(row["lease_until"])
                    and str(row["lease_until"]) < ts
                )
                if state == "approved" or expired:
                    self._refuse_unless_committed(row, checker)
                    async with self.conn.execute(
                        "UPDATE requests "
                        "SET state = 'running', owner = ?, lease_until = ?, updated_at = ? "
                        "WHERE id = ? AND ( state = 'approved' "
                        "   OR ( state = 'running' AND lease_until IS NOT NULL "
                        "        AND julianday(lease_until) < julianday(?) ) )",
                        (owner, until, ts, request_id, ts),
                    ) as cur:
                        won = cur.rowcount == 1
                    if not won:  # lost to another process between our read and here
                        fresh = await self._select(request_id) or row
                        raise ClaimRefused(
                            "leased", fresh["state"], fresh["lease_until"]
                        )
                    await self._event(
                        request_id,
                        ts,
                        "runner",
                        "claimed",
                        {"owner": owner, "lease_until": until},
                    )
                    await self.conn.execute("COMMIT")
                else:
                    reason = {"blocked": "blocked", "draft": "draft"}.get(
                        state, "leased" if state == "running" else "settled"
                    )
                    # The `except ClaimRefused` below rolls back; doing it here
                    # too is a second rollback on a closed transaction.
                    raise ClaimRefused(reason, state, row["lease_until"])
            except ClaimRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return await self.get_request(request_id) or {}

    @staticmethod
    def _refuse_unless_committed(
        row: dict[str, Any], checker: Callable[[str], bool | None]
    ) -> None:
        """§4.1's "only committed code may spend", at the moment it bites."""
        sha = row.get("code_ref")
        if not sha:
            raise ClaimRefused("code_ref_unresolved", row["state"])
        # `None` means *cannot tell*, not *not committed*: the image has no
        # checkout and its sha is the build arg. Recorded, not guessed at.
        if checker(str(sha)) is False:
            raise ClaimRefused("code_ref_unknown", row["state"], code_ref=str(sha))

    async def release(
        self,
        request_id: str,
        owner: str,
        *,
        reason: str | None = None,
        now: str | None = None,
    ) -> bool:
        """Give the row back, without waiting out a lease.

        The `AND owner = ?` is load-bearing: a runner that lost its lease to a
        reclaim must not be able to release the *new* claimant's row.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                async with self.conn.execute(
                    "UPDATE requests "
                    "SET state = 'approved', owner = NULL, lease_until = NULL, updated_at = ? "
                    "WHERE id = ? AND state = 'running' AND owner = ?",
                    (ts, request_id, owner),
                ) as cur:
                    released = cur.rowcount == 1
                if not released:
                    await self.conn.execute("ROLLBACK")
                    return False
                await self._event(
                    request_id, ts, "runner", "released", {"owner": owner, "reason": reason}
                )
                await self.conn.execute("COMMIT")
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return True

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
