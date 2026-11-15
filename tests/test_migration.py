"""The phase-2 table rebuild (§5.6): one migration, two changes, no data loss."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

from data_download.db import Store, _payload_hash_is_unique

TS = "2026-11-14T00:00:00Z"

# The shape the deployed store has today: `plan_json`/`plan_hash`, and a UNIQUE
# payload_hash. Written out in full because the point is to migrate the table
# that actually exists in the wild, not a simplification of it.
PHASE1_DDL = """
CREATE TABLE requests (
  id               TEXT PRIMARY KEY,
  state            TEXT NOT NULL,
  body_kind        TEXT NOT NULL,
  plan_type        TEXT,
  plan_json        TEXT,
  params_json      TEXT NOT NULL,
  payload_hash     TEXT NOT NULL UNIQUE,
  intent_json      TEXT,
  estimate_usd     REAL,
  blocked_reason   TEXT,
  plan_origin      TEXT,
  matched_profile  TEXT,
  code_ref         TEXT,
  plan_hash        TEXT,
  lease_until      TEXT,
  landed_sha256    TEXT,
  errors_json      TEXT,
  why              TEXT,
  max_usd          REAL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE TABLE events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id   TEXT NOT NULL REFERENCES requests(id),
  ts           TEXT NOT NULL,
  actor_kind   TEXT NOT NULL,
  event        TEXT NOT NULL,
  detail_json  TEXT
);
CREATE INDEX idx_requests_state ON requests(state);
"""


def _phase1_store(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(PHASE1_DDL)
        conn.execute(
            "INSERT INTO requests (id, state, body_kind, plan_type, plan_json, "
            "params_json, payload_hash, plan_hash, why, max_usd, created_at, updated_at) "
            "VALUES ('req_old', 'draft', 'plan', 'databento_batch_download', "
            "'{\"plan\": \"databento_batch_download\", \"steps\": []}', '{}', 'payload-1', "
            "'hash-1', 'the reason', 40, ?, ?)",
            (TS, TS),
        )
        conn.execute(
            "INSERT INTO events (request_id, ts, actor_kind, event, detail_json) "
            "VALUES ('req_old', ?, 'submitter', 'submitted', NULL)",
            (TS,),
        )
        conn.commit()
    finally:
        conn.close()


def _connect(path: Path) -> None:
    store = Store(str(path))

    async def go():
        await store.connect()
        await store.close()

    asyncio.run(go())


def _columns(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
    finally:
        conn.close()


def _row(path: Path, request_id: str = "req_old") -> sqlite3.Row:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        conn.close()


def test_an_existing_row_survives_the_rebuild(tmp_path):
    path = tmp_path / "deployed.db"
    _phase1_store(path)

    _connect(path)

    columns = _columns(path)
    assert {"job_json", "job_hash"} <= columns
    assert not {"plan_json", "plan_hash"} & columns

    row = _row(path)
    assert row["state"] == "draft"
    assert row["job_hash"] == "hash-1"  # renamed, not recomputed
    assert row["job_json"].startswith('{"plan": "databento_batch_download"')
    assert row["payload_hash"] == "payload-1"
    assert row["why"] == "the reason"
    assert row["max_usd"] == 40


def test_the_payload_hash_is_no_longer_unique(tmp_path):
    path = tmp_path / "deployed.db"
    _phase1_store(path)
    _connect(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO requests (id, state, body_kind, plan_type, job_json, "
            "params_json, payload_hash, job_hash, created_at, updated_at) VALUES "
            "('req_second', 'draft', 'plan', 'databento_batch_download', '{}', '{}', "
            "'payload-1', 'hash-1', ?, ?)",
            (TS, TS),
        )
        conn.commit()
        ids = [r["id"] for r in conn.execute("SELECT id FROM requests ORDER BY id")]
        # Two Requests, one body, one Job: that is the ruling (§5.6).
        assert ids == ["req_old", "req_second"]
        assert conn.execute(
            "SELECT COUNT(*) FROM requests WHERE payload_hash = 'payload-1'"
        ).fetchone()[0] == 2
    finally:
        conn.close()


def test_the_idempotency_key_is_still_exactly_once(tmp_path):
    path = tmp_path / "deployed.db"
    _phase1_store(path)
    _connect(path)

    conn = sqlite3.connect(path)
    try:
        for request_id in ("req_a", "req_b"):
            conn.execute(
                "INSERT INTO requests (id, state, body_kind, plan_type, job_json, "
                "params_json, payload_hash, idempotency_key, created_at, updated_at) "
                "VALUES (?, 'draft', 'plan', 'p', '{}', '{}', ?, 'attempt-1', ?, ?)",
                (request_id, f"payload-{request_id}", TS, TS),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        pass
    finally:
        conn.close()

    assert _row(path, "req_a") is not None
    assert _row(path, "req_b") is None


def test_the_views_are_rebuilt_on_the_new_columns(tmp_path):
    """`v_stuck` selected `plan_hash`; a stale view would fail at query time."""
    path = tmp_path / "deployed.db"
    _phase1_store(path)
    _connect(path)

    conn = sqlite3.connect(path)
    try:
        for index in range(3):
            conn.execute(
                "INSERT INTO requests (id, state, body_kind, plan_type, job_json, "
                "params_json, payload_hash, job_hash, created_at, updated_at) VALUES "
                "(?, 'failed', 'plan', 'p', '{}', '{}', ?, 'hash-same', ?, ?)",
                (f"req_f{index}", f"payload-f{index}", TS, TS),
            )
        conn.commit()
        conditions = [
            row[0] for row in conn.execute("SELECT condition FROM v_stuck")
        ]
        # Three rows sharing a job_hash are three deliberate filings (§5.5).
        assert "repeat_failure" in conditions
        # And the dashboard's union view still answers.
        assert conn.execute("SELECT COUNT(*) FROM v_requests").fetchone()[0] == 4
    finally:
        conn.close()


def test_the_migration_is_idempotent(tmp_path):
    path = tmp_path / "deployed.db"
    _phase1_store(path)

    _connect(path)
    first = _row(path)
    _connect(path)
    second = _row(path)

    assert dict(first) == dict(second)
    assert {"job_json", "job_hash"} <= _columns(path)


VIEWS_OVER_PHASE1 = """
CREATE VIEW v_pending AS SELECT id, state FROM requests WHERE state = 'draft';
CREATE VIEW v_done AS SELECT id FROM requests WHERE state = 'succeeded';
CREATE VIEW v_stuck AS SELECT e.id, e.event FROM events e;
"""


def test_a_deployed_store_migrates_with_its_views_still_standing(tmp_path):
    """The shape that shipped a 503: a phase-1 table *with* views over it.

    With `legacy_alter_table` off SQLite re-parses every view during a RENAME,
    so `ALTER TABLE requests_phase2 RENAME TO requests` died on the views left
    standing over the `requests` the rebuild had just dropped -- `error in view
    v_pending: no such table: main.requests`, three retries, then a store that
    never opened. Every other test here starts from a table with no views, which
    is why none of them saw it.
    """
    path = tmp_path / "deployed.db"
    _phase1_store(path)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(VIEWS_OVER_PHASE1)
        conn.commit()
    finally:
        conn.close()

    _connect(path)

    columns = _columns(path)
    assert {"job_json", "job_hash"} <= columns
    assert not {"plan_json", "plan_hash"} & columns
    assert _row(path)["job_hash"] == "hash-1"

    conn = sqlite3.connect(path)
    try:
        # The five views are back, on the new columns, and they answer.
        assert conn.execute("SELECT COUNT(*) FROM v_requests").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM v_stuck").fetchone()[0] == 0
    finally:
        conn.close()


def test_the_events_rebuild_also_survives_standing_views(tmp_path):
    """`v_stuck` reads `events`; its presence used to be enough to fail the RENAME."""
    path = tmp_path / "deployed.db"
    _phase1_store(path)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE VIEW v_stuck AS SELECT id, event FROM events")
        conn.commit()
    finally:
        conn.close()

    _connect(path)

    conn = sqlite3.connect(path)
    try:
        # The widened CHECK is the whole point of that rebuild.
        conn.execute(
            "INSERT INTO events (request_id, ts, actor_kind, event) "
            "VALUES ('req_old', ?, 'human', 'approved')",
            (TS,),
        )
        conn.commit()
        kinds = [r[0] for r in conn.execute("SELECT actor_kind FROM events")]
        assert "human" in kinds
        assert conn.execute(
            "SELECT SQL FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()[0].count("'human'") == 1
    finally:
        conn.close()


def test_the_primary_key_is_not_mistaken_for_a_unique_payload_hash(tmp_path):
    """A `TEXT PRIMARY KEY` makes a unique index too; the columns decide."""
    path = tmp_path / "fresh.db"
    _connect(path)

    async def ask() -> bool:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            return await _payload_hash_is_unique(conn)
        finally:
            await conn.close()

    assert asyncio.run(ask()) is False


def test_a_fresh_store_is_already_the_new_shape(tmp_path):
    path = tmp_path / "fresh.db"
    _connect(path)
    columns = _columns(path)
    assert {"job_json", "job_hash", "idempotency_key"} <= columns
    assert not {"plan_json", "plan_hash"} & columns
