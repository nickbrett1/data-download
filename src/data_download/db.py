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
import uuid
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
    # Phase 2.5: why an intent has no Job, and whether anyone has asked for a
    # plan. Nullable and added in place -- no CHECK widens, so no table rebuild:
    # the verdict is a column, not a new `blocked_reason` value.
    ("requests", "match_verdict", "TEXT"),
    ("requests", "match_report_json", "TEXT"),
    ("requests", "escalated_at", "TEXT"),
    # Archiving a settled row. Nullable and added in place: a column, not a
    # state, so the CHECK does not widen and no table rebuild is needed.
    ("requests", "archived_at", "TEXT"),
    # Phase 5.5: the request is waiting for a driver the loop is building.
    # Nullable and added in place -- like `archived_at`, a column rather than a
    # state, because the request's state is still honestly `approved`.
    ("requests", "driver_state", "TEXT"),
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
  match_verdict    TEXT,
  match_report_json TEXT,
  escalated_at     TEXT,
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

# `owner` is in this list even though a phase-1 table has no such column: the
# deployed store does, because `_add_missing_columns` ran on it before any
# rebuild could, and a lease holder must not vanish because a column the copy
# forgot was quietly reset to NULL.
_PHASE2_COLUMNS = (
    "id, state, body_kind, plan_type, job_json, params_json, payload_hash, "
    "idempotency_key, intent_json, estimate_usd, blocked_reason, plan_origin, "
    "matched_profile, code_ref, job_hash, owner, lease_until, landed_sha256, "
    "errors_json, match_verdict, match_report_json, escalated_at, "
    "why, max_usd, created_at, updated_at"
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
    # Same reason as the requests rebuild: `v_stuck` reads `events`, and the
    # RENAME re-parses it. See the comment in `_migrate_requests`.
    await _drop_views(conn)
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


async def _payload_hash_is_unique(conn: aiosqlite.Connection) -> bool:
    """True when a UNIQUE index covers `payload_hash` and nothing else.

    Asking `index_list` alone was not enough to mean this: a table's
    `TEXT PRIMARY KEY` also produces a unique autoindex, and "a unique autoindex
    exists" is then true of every store, including the ones this migration has
    already fixed. The columns decide it.
    """
    async with conn.execute("PRAGMA index_list(requests)") as cur:
        indexes = await cur.fetchall()
    for row in indexes:
        if not int(row["unique"]):
            continue
        async with conn.execute(f'PRAGMA index_info("{row["name"]}")') as info:
            columns = [r["name"] for r in await info.fetchall()]
        if columns == ["payload_hash"]:
            return True
    return False


def _needs_rebuild(columns: set[str], payload_hash_unique: bool) -> bool:
    """True when the table is still phase 1's shape (§5.6)."""
    if "plan_json" in columns or "plan_hash" in columns:
        return True
    if "job_json" not in columns:
        return True
    return payload_hash_unique


async def _migrate_requests(conn: aiosqlite.Connection) -> None:
    async with conn.execute("PRAGMA table_info(requests)") as cur:
        columns = {row["name"] for row in await cur.fetchall()}
    if not columns:  # a fresh database: the schema will create the new shape
        return

    if not _needs_rebuild(columns, await _payload_hash_is_unique(conn)):
        return

    log.info("rebuilding requests: plan_json/plan_hash -> job_json/job_hash, "
             "payload_hash loses UNIQUE")
    # The views go first, and this is the bug that cost a deploy: with
    # `legacy_alter_table` off (the default) SQLite re-parses every view during
    # a RENAME, so `ALTER TABLE requests_phase2 RENAME TO requests` fails on the
    # views left standing over the `requests` this transaction just dropped --
    # `error in view v_pending: no such table: main.requests`, on a database
    # with nothing wrong with it. A view is a query: dropping it loses nothing,
    # and `_apply_schema` puts all five back a few statements later.
    await _drop_views(conn)
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


def _rows_final_said_requests_ddl() -> str:
    """`CREATE TABLE requests ...` from schema.sql, under a name of its own.

    The rebuild below needs the *current* definition of the table, including
    every CHECK. Writing it out a second time is how the two copies drift: the
    one in this file would keep the old `state` list and happily copy a row into
    a table that cannot hold a `cancelled` one. So the schema is the single
    source, and this renames the table it creates.
    """
    text = SCHEMA_PATH.read_text(encoding="utf-8")
    marker = "CREATE TABLE IF NOT EXISTS requests ("
    start = text.index(marker)
    end = text.index("\n);", start) + 3
    return text[start:end].replace(marker, "CREATE TABLE requests_widened (", 1)


async def _learn_cancelled_state(conn: aiosqlite.Connection) -> None:
    """Teach an existing store the word `cancelled`, and forget `withdrawn`.

    Two jobs in one rebuild, because SQLite cannot do either in place: the
    `state` CHECK has to name the new value, and rows written under the old word
    have to be copied across as the new one. Doing them apart would mean either
    a table that refuses the value or a store where the same decision is spelled
    two ways.

    SQLite has no `ALTER TABLE ... ALTER CONSTRAINT`, so a new value in a CHECK
    costs a rebuild. Idempotent by inspection: if the live table's own SQL
    already names the value, there is nothing to do -- which is why this is a
    check on `sqlite_master.sql` rather than a version number nothing else
    keeps. Copying is by column *intersection* with the live table, so a store
    one phase behind gains nothing it does not have (those columns are added by
    `_add_missing_columns`, which runs first).

    The views are dropped first for the reason `_migrate_requests` documents at
    length: with `legacy_alter_table` off, SQLite re-parses every view during a
    RENAME and fails on a view left standing over the table this transaction
    just dropped. They come back in `_apply_schema` -- and that is the second
    thing this migration buys: `v_done` is `CREATE VIEW IF NOT EXISTS`, so a
    deployed store would otherwise keep a definition that cannot show a
    cancelled row.
    """
    async with conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'requests'"
    ) as cur:
        row = await cur.fetchone()
    if row is None or not row["sql"] or "'cancelled'" in row["sql"]:
        return

    log.info("rebuilding requests: the state CHECK learns 'cancelled'")
    await _drop_views(conn)
    await conn.execute("PRAGMA foreign_keys=OFF")
    try:
        await conn.execute("BEGIN IMMEDIATE")
        await conn.execute("DROP TABLE IF EXISTS requests_widened")
        await conn.execute(_rows_final_said_requests_ddl())
        async with conn.execute("PRAGMA table_info(requests)") as cur:
            columns = [r["name"] for r in await cur.fetchall()]
        names = ", ".join(columns)
        # `state` is the one column this migration changes on the way across:
        # the row that said `withdrawn` says `cancelled` now, or the insert would
        # be refused by the very CHECK this rebuild exists to install. Events are
        # left alone -- the trail is append-only, and one 2026-11-22 entry
        # reading "withdrawn" is that row's history, not a typo.
        copied = ", ".join(
            "CASE WHEN state = 'withdrawn' THEN 'cancelled' ELSE state END"
            if column == "state"
            else column
            for column in columns
        )
        await conn.execute(
            f"INSERT INTO requests_widened ({names}) SELECT {copied} FROM requests"
        )
        await conn.execute("DROP TABLE requests")
        await conn.execute("ALTER TABLE requests_widened RENAME TO requests")
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

# Where a run can end (§3.1). `running` is the claimant's, not the runner's, and
# the three below are the only states `settle` may write.
TERMINAL_STATES = frozenset({"succeeded", "landed_with_errors", "failed"})

# What `archive` accepts: rows that are finished, in whatever way. `cancelled` is
# in it because a cancelled draft is finished too -- and because the two ways of
# leaving the book should not need two different verbs to come back.
ARCHIVABLE_STATES = TERMINAL_STATES | {"cancelled"}


class CancelRefused(Exception):
    """`cancel` said no, with one of a closed set of reasons."""

    REASONS = frozenset({"not_found", "wrong_state"})

    def __init__(self, reason: str, state: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state

    def as_text(self) -> str:
        return f"cancel_refused: {self.reason} (state={self.state})"


class ApproveRefused(Exception):
    """`approve` said no, with one of a closed set of reasons (phase 3 §2)."""

    REASONS = frozenset({"not_found", "blocked", "wrong_state", "job_missing"})

    def __init__(self, reason: str, state: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state

    def as_text(self) -> str:
        return f"approve_refused: {self.reason} (state={self.state})"


class RematchRefused(Exception):
    """A row cannot be re-matched (phase 2.6).

    `not_blocked` is the important one: a row that already has a Job has
    committed to one, and a Job is write-once. A new plan version implies a
    different Job, which is a new Request, not an edit of this one.
    """

    REASONS = frozenset({"not_found", "not_blocked"})

    def __init__(self, reason: str, state: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state

    def as_text(self) -> str:
        return f"rematch_refused: {self.reason} (state={self.state})"


class EscalateRefused(Exception):
    """A caller asked for a new plan and the answer was no (phase 2.5).

    `would_fit` is carried, not just the reason: the only useful thing to say
    to a caller whose intent an existing plan could take is *how* to make it
    take it, and making them ask twice to find out is how a caller learns that
    escalating is easier than reading.
    """

    REASONS = frozenset(
        {"not_found", "wrong_state", "a_plan_matches", "already_requested"}
    )

    def __init__(
        self,
        reason: str,
        state: str | None = None,
        would_fit: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state
        self.would_fit = would_fit

    def as_text(self) -> str:
        return f"escalate_refused: {self.reason} (state={self.state})"


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


async def _drop_views(conn: aiosqlite.Connection) -> None:
    """Throw the five read-surface views away; `_apply_schema` recreates them."""
    for name in VIEW_NAMES:
        await conn.execute(f"DROP VIEW IF EXISTS {name}")


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
            await _learn_cancelled_state(conn)
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
        states: list[str] | tuple[str, ...] | None = None,
        plan_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        args: list[Any] = []
        if state:
            where.append("state = ?")
            args.append(state)
        elif states:
            # One query for the datasets browser, which wants every state that
            # can have bytes. The single-`state` form stays for its callers.
            wanted = list(dict.fromkeys(states))
            where.append(f"state IN ({', '.join('?' for _ in wanted)})")
            args.extend(wanted)
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
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """`draft` -> `approved`. Returns `(row, changed)`.

        Approving an already-approved row is a **no-op with no second event**: a
        retried CLI call must not be able to inflate the record, and one
        approval is one row or the audit trail is not an audit trail.

        `context` is recorded as-is beside the actor, note and job hash. The CLI
        passes nothing; the HTTP door passes `{"via": "serve"}`, so an approval
        can be told apart from a shell one. There is no credential to
        fingerprint any more: the actor *is* the tailnet login Serve asserted.
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
                    {
                        "actor": actor,
                        "note": note,
                        "job_hash": row["job_hash"],
                        **(context or {}),
                    },
                )
                await self.conn.execute("COMMIT")
            except ApproveRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return (await self.get_request(request_id) or row), True

    async def cancel(
        self,
        request_id: str,
        actor: str,
        note: str | None = None,
        *,
        unbindable: bool = False,
        now: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """`draft` (or `blocked`) -> `cancelled`. Returns `(row, changed)`.

        Cancelling is a decision about a row **nobody has run**: the draft that
        was filed twice, the intent that is never going to get a plan. It is not
        a delete -- the row, its quote, its events and the `why` a person typed
        all stay, and the badge changes. A deleted row would leave a hole where
        a filing used to be, and "I changed my mind" is a fact about the
        request, not a correction of the record.

        It is not the inverse of approval either. Only `draft` is accepted:
        `approved` is refused because an approval authorises one attempt, and
        once the runner may hold the lease, stopping the work is the runner's
        business -- writing `cancelled` under a running claim would be this
        process claiming a cancellation it cannot enforce. Everything past that
        is history and answers to `retry`, not to `cancel`.

        `blocked` is refused for a duller reason: two CHECKs encode "a blocked
        row has no Job and no estimate", so a cancelled row that never had one
        would need both relaxed. That is a schema decision about what a
        never-bound row is, and taking it here would smuggle it in.

        Cancelling twice is a no-op with no second event, for the reason
        approval is: a retried call must not be able to inflate the record.

        `unbindable=True` is the one widening, and the caller has to establish
        it: it says the plan type this row names is no longer in git, so no
        binder exists, so no runner can ever hold the lease on it. An approval
        that authorises an attempt nobody can make is not "work in flight" --
        the reason `approved` is refused -- it is an authorisation for nothing,
        and leaving it in the queue forever makes the queue lie about what it
        holds. The store cannot check this itself (it has no registry), so it
        refuses nothing and trusts the caller's assertion; the route is where
        the registry lives.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise CancelRefused("not_found")
                state = row["state"]
                if state == "cancelled":
                    await self.conn.execute("ROLLBACK")
                    return row, False
                if state != "draft" and not (unbindable and state == "approved"):
                    raise CancelRefused("wrong_state", state)
                await self.conn.execute(
                    "UPDATE requests SET state = 'cancelled', updated_at = ? "
                    "WHERE id = ? AND state = ?",
                    (ts, request_id, state),
                )
                await self._event(
                    request_id,
                    ts,
                    "human",
                    "cancelled",
                    {
                        "actor": actor,
                        "note": note,
                        # From which state it was cancelled, because `blocked`
                        # and `draft` mean different things about how far this
                        # request ever got.
                        "was": state,
                        # Present only when an approval was withdrawn because
                        # nothing could ever run it: the record should say so,
                        # or the event reads as a person stopping live work.
                        **({"unbindable": True} if state == "approved" else {}),
                        **(context or {}),
                    },
                )
                await self.conn.execute("COMMIT")
            except CancelRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return (await self.get_request(request_id) or row), True

    async def archive(
        self,
        request_id: str,
        actor: str,
        note: str | None = None,
        *,
        archived: bool = True,
        now: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Take a settled row off the book, or put it back. Returns `(row, changed)`.

        This is the verb a `failed` row needs, and it is deliberately *not*
        `cancel`. Cancelling rewrites `state`, and a failed attempt's state is
        the outcome of something that really happened -- the vendor was asked,
        the driver refused, the errors are on the row. Setting that to
        `cancelled` would make the record claim the request was cancelled before
        it ran, which is not true and is not repairable after the fact.

        So the row keeps its state and gains a date. The book leaves it out of
        the default list (a finished row nobody is looking at is noise next to
        one that needs a person), and it stays reachable by name in the
        `archived` facet. Restoring clears the date; both directions are
        idempotent, with no second event, for the reason approval is.

        Only settled rows are accepted. A `draft` is cancelled, not archived: it
        is still a decision that has not been made. `approved` and `running` are
        in flight, and hiding something that may still spend is the one thing a
        list of "what is happening" must not do.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise CancelRefused("not_found")
                state = row["state"]
                if state not in ARCHIVABLE_STATES:
                    raise CancelRefused("wrong_state", state)
                already = row["archived_at"] is not None
                if already == archived:
                    await self.conn.execute("ROLLBACK")
                    return row, False
                await self.conn.execute(
                    "UPDATE requests SET archived_at = ?, updated_at = ? WHERE id = ?",
                    (ts if archived else None, ts, request_id),
                )
                await self._event(
                    request_id,
                    ts,
                    "human",
                    "archived" if archived else "restored",
                    {"actor": actor, "note": note, **(context or {})},
                )
                await self.conn.execute("COMMIT")
            except CancelRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return (await self.get_request(request_id) or row), True

    async def escalate(
        self,
        request_id: str,
        *,
        note: str | None = None,
        now: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """`blocked` -> "a new plan has been asked for". Returns `(row, changed)`.

        This records **demand, not a plan**: a plan type is a params schema plus
        a committed binder, and nothing here writes one. What it does is turn a
        row that nobody was looking at into a row with an owner's request on it,
        and it carries the match report so the request is evidence-backed.

        It is refused while any registered plan is merely short of parameters --
        `a_plan_matches` -- because that is a caller who has not finished
        reading, not a gap in the registry. Escalating twice is a no-op with no
        second event, for the same reason approval is.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise EscalateRefused("not_found")
                if row["state"] != "blocked":
                    raise EscalateRefused("wrong_state", row["state"])
                if row["escalated_at"]:
                    await self.conn.execute("ROLLBACK")
                    return row, False
                report = (
                    json.loads(row["match_report_json"])
                    if row["match_report_json"]
                    else {}
                )
                if row["match_verdict"] == "needs_params":
                    raise EscalateRefused(
                        "a_plan_matches", row["state"], report.get("would_fit")
                    )
                if row["match_verdict"] == "matched":
                    # A bound row is not blocked; a hand-edited store is not
                    # bound by a state CHECK written when the row was stored.
                    raise EscalateRefused("a_plan_matches", row["state"], None)
                await self.conn.execute(
                    "UPDATE requests SET escalated_at = ?, updated_at = ? "
                    "WHERE id = ? AND state = 'blocked' AND escalated_at IS NULL",
                    (ts, ts, request_id),
                )
                await self._event(
                    request_id,
                    ts,
                    "submitter",
                    "escalated",
                    {
                        "note": note,
                        "match_verdict": row["match_verdict"],
                        "registry": report.get("registry"),
                        "would_fit": report.get("would_fit"),
                    },
                )
                await self.conn.execute("COMMIT")
            except EscalateRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return (await self.get_request(request_id) or row), True

    # -- phase 2.6: re-running the match when the registry moves ------------

    async def blocked_ids(self, ids: list[str] | None = None) -> list[str]:
        """Every blocked Request, oldest first (or the blocked subset of `ids`).

        Oldest first because that is the order a reconciliation should work in:
        the row that has been waiting longest is the one whose caller most wants
        an answer, and it makes the summary log read like the queue.
        """
        sql = "SELECT id FROM requests WHERE state = 'blocked'"
        args: list[Any] = []
        if ids:
            sql += f" AND id IN ({', '.join('?' for _ in ids)})"
            args = list(ids)
        sql += " ORDER BY created_at ASC, id ASC"
        async with self.conn.execute(sql, args) as cur:
            return [str(row["id"]) for row in await cur.fetchall()]

    async def approved_ids(self, limit: int = 100) -> list[str]:
        """The queue: every approved Request, oldest first.

        A method rather than a caller sorting `list_requests`, because that
        surface is newest-first for the page and its `limit` is applied *before*
        any re-sort -- so a caller that sorts afterwards still takes the newest
        N and calls the oldest of them first. On a backlog larger than the
        window, that starves the request that has waited longest, which is the
        one ordering rule a queue has.

        Ties break on id, so two rows filed in the same second have one order
        forever rather than whatever the storage engine returns today.
        """
        async with self.conn.execute(
            "SELECT id FROM requests WHERE state = 'approved' "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (limit,),
        ) as cur:
            return [str(row["id"]) for row in await cur.fetchall()]

    async def rematch(
        self,
        request_id: str,
        decide: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        now: str | None = None,
        dry_run: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Recompute one blocked row's verdict against the registry in force.

        `decide` is pure and is handed the row **as stored, inside the write
        transaction**: it returns `None` when the stored verdict is not stale
        (writes nothing) or a dict with the new `verdict` and `report_json`,
        plus `job_text`, `job_digest`, `plan_type` and `code_ref` when the row
        now binds. Keeping the match inside the transaction is deliberate: the
        verdict is written together with the Job it justifies or not at all.

        Returns `(before, after, changed)`. On a dry run nothing is written and
        `after` is what the row *would* become, but `changed` still means "a real
        run would write this one": the summary a person wants before a deploy
        sweeps a hundred rows into `draft`.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._select(request_id)
                if row is None:
                    raise RematchRefused("not_found")
                if row["state"] != "blocked":
                    raise RematchRefused("not_blocked", row["state"])
                decision = decide(row)
                if decision is None:
                    await self.conn.execute("ROLLBACK")
                    return row, row, False

                verdict = decision["verdict"]
                binds = decision.get("job_text") is not None
                if dry_run:
                    await self.conn.execute("ROLLBACK")
                    return (
                        row,
                        {
                            **row,
                            "state": "draft" if binds else "blocked",
                            "match_verdict": verdict,
                            "plan_type": decision.get("plan_type"),
                        },
                        True,
                    )

                if binds:
                    await self.conn.execute(
                        "UPDATE requests SET state = 'draft', plan_type = ?, "
                        "job_json = ?, job_hash = ?, blocked_reason = NULL, "
                        "plan_origin = 'rematched', code_ref = ?, "
                        "match_verdict = ?, match_report_json = ?, updated_at = ? "
                        "WHERE id = ? AND state = 'blocked'",
                        (
                            decision["plan_type"],
                            decision["job_text"],
                            decision["job_digest"],
                            decision.get("code_ref"),
                            verdict,
                            decision["report_json"],
                            ts,
                            request_id,
                        ),
                    )
                else:
                    await self.conn.execute(
                        "UPDATE requests SET match_verdict = ?, "
                        "match_report_json = ?, updated_at = ? "
                        "WHERE id = ? AND state = 'blocked'",
                        (verdict, decision["report_json"], ts, request_id),
                    )
                # One event per row that moved. A digest-only refresh is
                # bookkeeping and would otherwise fill the trail with entries
                # that say "nothing happened" once per deploy.
                if binds or row["match_verdict"] != verdict:
                    await self._event(
                        request_id,
                        ts,
                        "system",
                        "rematched",
                        {
                            "from_verdict": row["match_verdict"],
                            "to_verdict": verdict,
                            "registry": decision.get("registry"),
                            "bound": binds,
                            "job_hash": decision.get("job_digest"),
                            "plan_type": decision.get("plan_type"),
                            # The row's provenance moves with the Job: this code
                            # bound it, so this is the commit a claim checks.
                            "code_ref": decision.get("code_ref"),
                        },
                    )
                await self.conn.execute("COMMIT")
            except RematchRefused:
                await self.conn.execute("ROLLBACK")
                raise
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        after = await self.get_request(request_id) or row
        return row, after, True

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

    async def renew(
        self,
        request_id: str,
        owner: str,
        *,
        lease_seconds: int | None = None,
        now: str | None = None,
    ) -> bool:
        """Push the lease forward while a run is still working. False: it is not ours.

        A lease is a promise that *someone is working on this*, not an estimate of
        how long the work takes. The 900 s default was doing both jobs, and a
        healthy run of 120 polls at 30 s (≈ 3600 s) outlives it -- so the row is
        reclaimed while the first attempt is still spending, and the vendor
        charges twice. A longer lease is not the fix: it would keep a *crashed*
        claimant's row frozen for as long as the worst case, and the whole point
        of the lease is that a dead process gives its rows back quickly.

        So the leaseholder says "still here" on a heartbeat instead. Same
        discipline as `settle`: `AND state = 'running' AND owner = ?`. `False`
        means the row was reclaimed (or settled by somebody else) and this
        attempt must stop before it spends any more -- not raise, because losing
        the lease is the reclaim working, not an error.
        """
        lease = LEASE_SECONDS if lease_seconds is None else lease_seconds
        ts = now or utcnow()
        until = shift_seconds(ts, lease)
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                async with self.conn.execute(
                    "UPDATE requests SET lease_until = ? "
                    "WHERE id = ? AND state = 'running' AND owner = ?",
                    (until, request_id, owner),
                ) as cur:
                    renewed = cur.rowcount == 1
                if not renewed:
                    await self.conn.execute("ROLLBACK")
                    return False
                # No event per beat: a heartbeat is bookkeeping, and a 40-minute
                # run would otherwise write a hundred rows that all say the same
                # thing and bury the events that say something.
                await self.conn.execute("COMMIT")
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        return True

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

    async def settle(
        self,
        request_id: str,
        owner: str,
        state: str,
        *,
        landed_sha256: str | None = None,
        errors: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> dict[str, Any]:
        """`running` -> a terminal state, by the leaseholder and nobody else.

        Same CAS discipline as `claim` and `release`: `AND state = 'running'
        AND owner = ?` is load-bearing. A runner whose lease expired and was
        reclaimed must not be able to write `succeeded` over the new claimant's
        row, even though it really did finish the old attempt.

        Terminal states are `succeeded`, `landed_with_errors` and `failed`
        (§3.1); `landed` is written for the two that put bytes on disk and
        `failed` for the one that did not, so the trail reads as what happened
        rather than as a state machine's name.

        Returns the row after the write, or the unchanged row with
        `"settled": False` when the CAS lost -- the caller must not treat a
        refused settle as an error: it is the reclaim working.
        """
        if state not in TERMINAL_STATES:
            raise ValueError(
                f"settle to {state!r} is not terminal; expected one of "
                f"{', '.join(sorted(TERMINAL_STATES))}"
            )
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                async with self.conn.execute(
                    "UPDATE requests "
                    "SET state = ?, owner = ?, lease_until = NULL, updated_at = ?, "
                    "    landed_sha256 = COALESCE(?, landed_sha256), "
                    "    errors_json = ? "
                    "WHERE id = ? AND state = 'running' AND owner = ?",
                    (
                        state,
                        owner,
                        ts,
                        landed_sha256,
                        canonical_json(errors) if errors else None,
                        request_id,
                        owner,
                    ),
                ) as cur:
                    settled = cur.rowcount == 1
                if not settled:
                    await self.conn.execute("ROLLBACK")
                    row = await self.get_request(request_id) or {}
                    row["settled"] = False
                    return row
                await self._event(
                    request_id,
                    ts,
                    "runner",
                    "landed" if state != "failed" else "failed",
                    {
                        "owner": owner,
                        "state": state,
                        "landed_sha256": landed_sha256,
                        "errors": errors,
                        **(detail or {}),
                    },
                )
                await self.conn.execute("COMMIT")
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise
        row = await self.get_request(request_id) or {}
        row["settled"] = True
        return row

    # -- the driver queue (phase 5.5) --------------------------------------

    #: States a job can be in and still be worth a dispatcher's attention. A job
    #: is "open" until it is installed or a human has been asked to look.
    OPEN_DRIVER_STATES = ("queued", "building", "gated", "probing", "merging", "installing")

    async def driver_job_enqueue(
        self,
        *,
        vendor: str,
        plan_types: list[str] | None = None,
        dataset: str | None = None,
        request_id: str | None = None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Queue a driver build for `vendor`, or return `None` if one is open.

        `INSERT OR IGNORE` against the partial unique index is the dedupe: ten
        requests for a vendor nobody drives are one driver to write. The caller
        is not told off for the second one -- it is the same answer.
        """
        job_id = "drv_" + uuid.uuid4().hex
        ts = now or utcnow()
        async with self._write_lock:
            async with self.conn.execute(
                "INSERT OR IGNORE INTO driver_jobs "
                "(id, vendor, state, plan_types_json, dataset, request_id, attempts, "
                " created_at, updated_at) VALUES (?, ?, 'queued', ?, ?, ?, 0, ?, ?)",
                (
                    job_id,
                    vendor,
                    canonical_json(list(plan_types or [])),
                    dataset,
                    request_id,
                    ts,
                    ts,
                ),
            ) as cur:
                created = cur.rowcount == 1
            await self.conn.commit()
        if not created:
            return None
        return await self.driver_job_get(job_id)

    async def driver_job_get(self, job_id: str) -> dict[str, Any] | None:
        async with self.conn.execute(
            "SELECT * FROM driver_jobs WHERE id = ?", (job_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def driver_job_next(self) -> dict[str, Any] | None:
        """The oldest queued job, or `None`. The dispatcher runs to exit."""
        async with self.conn.execute(
            "SELECT * FROM driver_jobs WHERE state = 'queued' "
            "ORDER BY created_at, id LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def driver_job_list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM driver_jobs ORDER BY created_at DESC, id LIMIT ?", (limit,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def driver_jobs_open(self) -> int:
        """How many jobs are still being worked on.

        The pages poll while a request is in flight; a request waiting for a
        driver is not in flight by the requests table's own vocabulary, so this
        is the second half of "something is moving".
        """
        marks = ", ".join("?" for _ in self.OPEN_DRIVER_STATES)
        async with self.conn.execute(
            f"SELECT COUNT(*) AS n FROM driver_jobs WHERE state IN ({marks})",
            self.OPEN_DRIVER_STATES,
        ) as cur:
            row = await cur.fetchone()
        return int(row["n"] if row else 0)

    async def driver_job_update(
        self, job_id: str, *, now: str | None = None, **fields: Any
    ) -> dict[str, Any] | None:
        """Move a job along. Only the columns the state machine owns."""
        allowed = {
            "state",
            "branch",
            "commit_sha",
            "artifact_sha256",
            "verdict_json",
            "last_error",
            "attempts",
            "request_id",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"driver_jobs has no column {sorted(unknown)[0]!r}")
        if not fields:
            return await self.driver_job_get(job_id)
        assignments = ", ".join(f"{name} = ?" for name in fields)
        async with self._write_lock:
            await self.conn.execute(
                f"UPDATE driver_jobs SET {assignments}, updated_at = ? WHERE id = ?",
                (*fields.values(), now or utcnow(), job_id),
            )
            await self.conn.commit()
        return await self.driver_job_get(job_id)

    async def note_driver_state(
        self,
        request_id: str,
        value: str | None,
        *,
        detail: dict[str, Any] | None = None,
        now: str | None = None,
    ) -> None:
        """Say what is happening to this request's driver, and record it.

        A column rather than a state: the request is still honestly `approved`
        while a driver is being written, and moving it to a pseudo-state would
        make every query that asks "what is waiting on me" need to know about
        the loop.
        """
        ts = now or utcnow()
        async with self._write_lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.conn.execute(
                    "UPDATE requests SET driver_state = ?, updated_at = ? WHERE id = ?",
                    (value, ts, request_id),
                )
                await self._event(
                    request_id,
                    ts,
                    "system",
                    "driver_state",
                    {"driver_state": value, **(detail or {})},
                )
                await self.conn.execute("COMMIT")
            except Exception:
                await self.conn.execute("ROLLBACK")
                raise

    # -- long poll ---------------------------------------------------------

    async def catalogue_get(
        self, pairs: list[tuple[str, str]]
    ) -> list[dict[str, Any]]:
        """The cached catalogue entries for some `(vendor, dataset)` pairs.

        One query with one `OR` per pair rather than a query per row: the pages
        that call this pass every dataset they are about to draw.
        """
        wanted = [(str(v), str(d)) for v, d in pairs if v and d]
        if not wanted:
            return []
        clause = " OR ".join("(vendor = ? AND dataset = ?)" for _ in wanted)
        args = [value for pair in wanted for value in pair]
        async with self.conn.execute(
            f"SELECT * FROM dataset_catalogue WHERE {clause}", args
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def catalogue_put(self, row: dict[str, Any]) -> None:
        """Insert or replace one description. Last write wins: it is a cache."""
        async with self._write_lock:
            await self.conn.execute(
                "INSERT INTO dataset_catalogue "
                "(vendor, dataset, description, described, venue, venues_json, "
                " range_json, schemas_json, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(vendor, dataset) DO UPDATE SET "
                "  description = excluded.description, "
                "  described = excluded.described, "
                "  venue = excluded.venue, "
                "  venues_json = excluded.venues_json, "
                "  range_json = excluded.range_json, "
                "  schemas_json = excluded.schemas_json, "
                "  fetched_at = excluded.fetched_at",
                (
                    row["vendor"],
                    row["dataset"],
                    row["description"],
                    int(row.get("described") or 0),
                    row.get("venue"),
                    row.get("venues_json"),
                    row.get("range_json"),
                    row.get("schemas_json"),
                    row["fetched_at"],
                ),
            )
            await self.conn.commit()

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
