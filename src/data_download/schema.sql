-- Phase 1 request store.
--
-- Deviations from `data-download-interface-and-operations` §8.1 are deliberate
-- and limited to what a `blocked` intent row needs (§7.9.2): `plan_json` and
-- `estimate_usd` are nullable, with the invariant stated as a CHECK instead of
-- NOT NULL. `estimate_usd` is always NULL this phase because nothing prices
-- anything.
--
-- Columns §8.1 carries that this phase does not need yet (`max_usd`, `why`,
-- `actual_usd`, `owner`, `sdk_version`, `blocked_since`, `author_attempts`) are
-- not columns here. `max_usd` and `why` live inside `intent_json`, which is the
-- verbatim body (§7.9.1); the others arrive with the phases that use them.

CREATE TABLE IF NOT EXISTS requests (
  id               TEXT PRIMARY KEY,
  state            TEXT NOT NULL,          -- draft|blocked|approved|running|succeeded|landed_with_errors|failed
  body_kind        TEXT NOT NULL,          -- plan|intent
  plan_type        TEXT,                   -- NULL when body_kind='intent'
  plan_json        TEXT,                   -- NULL iff state='blocked'
  params_json      TEXT NOT NULL,
  payload_hash     TEXT NOT NULL UNIQUE,   -- idempotence
  intent_json      TEXT,                   -- NULL when body_kind='plan'
  estimate_usd     REAL,                   -- NULL iff state='blocked'; nothing populates it this phase
  blocked_reason   TEXT,                   -- NULL unless state='blocked'
  plan_origin      TEXT,                   -- profile|resolved|adhoc
  matched_profile  TEXT,
  code_ref         TEXT,                   -- git sha; phase 3
  plan_hash        TEXT,
  lease_until      TEXT,                   -- phase 3
  landed_sha256    TEXT,                   -- phase 5
  errors_json      TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  CHECK (state IN ('draft','blocked','approved','running','succeeded','landed_with_errors','failed')),
  CHECK (body_kind IN ('plan','intent')),
  CHECK ((state = 'blocked') = (plan_json IS NULL)),
  -- The order prints this as `(state='blocked') = (estimate_usd IS NULL)`,
  -- which forces a non-NULL estimate on every draft row - but phase 1 prices
  -- nothing and the prose says the estimate is always NULL. Written as an
  -- implication instead: a blocked row must have no estimate, everything else
  -- is free to be priced by a later phase without a migration.
  CHECK (state <> 'blocked' OR estimate_usd IS NULL),
  CHECK (blocked_reason IS NULL OR state = 'blocked'),
  CHECK ((body_kind = 'intent') = (intent_json IS NOT NULL)),
  CHECK (blocked_reason IS NULL OR blocked_reason IN
         ('awaiting_plan','plan_rejected','resolver_stalled','ceiling_exceeded'))
);

-- Append-only. This phase writes exactly one `submitted` event per accepted
-- request and nothing else; there is no UPDATE or DELETE path.
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id   TEXT NOT NULL REFERENCES requests(id),
  ts           TEXT NOT NULL,
  actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('submitter','runner','system')),
  event        TEXT NOT NULL,
  detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_state       ON requests(state);
CREATE INDEX IF NOT EXISTS idx_requests_plan_type   ON requests(plan_type);
CREATE INDEX IF NOT EXISTS idx_events_request_id    ON events(request_id, id);

-- ---------------------------------------------------------------------------
-- Read surface (§7.8). The dashboard issues only SELECTs against these views.
-- ---------------------------------------------------------------------------

-- What is waiting on me. `approved_unclaimed` has no `owner` column to inspect
-- yet (that arrives with the claim in phase 3), so it is simply "approved".
CREATE VIEW IF NOT EXISTS v_pending AS
SELECT id,
       state,
       plan_type,
       plan_origin,
       estimate_usd,
       created_at,
       CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_seconds,
       CASE WHEN state = 'approved' THEN 1 ELSE 0 END AS approved_unclaimed
  FROM requests
 WHERE state IN ('draft','approved')
 ORDER BY created_at ASC;

-- What got done. Only the three terminal states appear. There is no
-- `actual_usd` column this phase, so the estimate is shown without a
-- comparison; `had_errors` is the flag that keeps `landed_with_errors` from
-- reading as `succeeded`.
CREATE VIEW IF NOT EXISTS v_done AS
SELECT id,
       state,
       plan_type,
       plan_origin,
       estimate_usd,
       created_at,
       updated_at,
       CASE WHEN state = 'landed_with_errors' THEN 1 ELSE 0 END AS had_errors,
       CASE WHEN state = 'succeeded' THEN 1 ELSE 0 END AS landed_clean
  FROM requests
 WHERE state IN ('succeeded','landed_with_errors','failed')
 ORDER BY updated_at DESC, rowid DESC;

-- What is broken. Exactly four conditions, all breakage (§7.8.1):
--   1. approved_unclaimed - approved past the claim window and nobody picked it up
--   2. lease_expired      - claimed, lease ran out, the claimant died mid-run
--   3. running_too_long   - running with a valid lease but no progress for >6h
--   4. repeat_failure     - one PLAN_HASH has >=3 failed rows
--
-- Condition 4 groups on `plan_hash`, not `payload_hash` as §7.8.1 prints it.
-- `payload_hash` is UNIQUE (idempotence), so a second row with the same
-- payload_hash can never exist and the condition as written is unreachable
-- forever - not merely in phase 1. The plan is what "never works", and a
-- re-filed request with a different `why` has a different payload_hash but the
-- same plan_hash. Recorded in the phase-1 Result as a design finding.
-- `blocked` is deliberately NOT here: waiting on a plan is not breakage
-- (§7.9.4), it is `v_blocked`.
--
-- Thresholds are literals because there is nowhere else to put them this
-- phase: 900 s claim window, 6 h no-progress, 3 failed rows.
CREATE VIEW IF NOT EXISTS v_stuck AS
SELECT request_id, state, condition, age_seconds, detail FROM (
  SELECT id AS request_id,
         state,
         'approved_unclaimed' AS condition,
         CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_seconds,
         'approved but never claimed (older than the claim window)' AS detail
    FROM requests
   WHERE state = 'approved'
     AND (julianday('now') - julianday(created_at)) * 86400 > 900
  UNION ALL
  SELECT id,
         state,
         'lease_expired',
         CAST((julianday('now') - julianday(lease_until)) * 86400 AS INTEGER),
         'claimed, lease expired - the claimant died mid-run'
    FROM requests
   WHERE state = 'running'
     AND lease_until IS NOT NULL
     AND julianday(lease_until) < julianday('now')
  UNION ALL
  SELECT id,
         state,
         'running_too_long',
         CAST((julianday('now') - julianday(updated_at)) * 86400 AS INTEGER),
         'running with a valid lease but no progress for over 6h'
    FROM requests
   WHERE state = 'running'
     AND lease_until IS NOT NULL
     AND julianday(lease_until) >= julianday('now')
     AND (julianday('now') - julianday(updated_at)) * 24 > 6
  UNION ALL
  SELECT r.id,
         r.state,
         'repeat_failure',
         CAST((julianday('now') - julianday(r.created_at)) * 86400 AS INTEGER),
         'plan_hash failed ' || g.cnt || ' times - this plan never works'
    FROM requests r
    JOIN (SELECT plan_hash, COUNT(*) AS cnt, MAX(created_at) AS latest
            FROM requests
           WHERE state = 'failed'
             AND plan_hash IS NOT NULL
           GROUP BY plan_hash
          HAVING COUNT(*) >= 3) g
      ON r.plan_hash = g.plan_hash
     AND r.created_at = g.latest
);

-- What is stuck waiting for a plan. Deliberately separate from v_stuck: this
-- is uniformly *not* alarming, which is what makes it safe to ignore for a
-- day. `merge_can_clear` is true only for `awaiting_plan` (§7.9.2).
CREATE VIEW IF NOT EXISTS v_blocked AS
SELECT id,
       blocked_reason,
       created_at,
       CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_seconds,
       json_extract(intent_json, '$.max_usd') AS max_usd,
       CASE WHEN blocked_reason = 'awaiting_plan' THEN 1 ELSE 0 END AS merge_can_clear
  FROM requests
 WHERE state = 'blocked'
 ORDER BY created_at ASC;

-- One row per request, for the dashboard's single list.
--
-- The four views above answer four different questions, which is why they are
-- shaped differently and cannot be UNIONed into one answer. But a reader
-- landing on the page wants one list, not four, so this view is the union of
-- *subjects* (every request) with the condition that makes it interesting
-- attached as an attribute rather than used as a partition.
--
-- `stuck_condition` is MIN(condition) because the four conditions are mutually
-- exclusive by state today (approved / running / running / failed); the GROUP
-- BY is there so a future overlap cannot duplicate a row in the list.
CREATE VIEW IF NOT EXISTS v_requests AS
SELECT r.id,
       r.rowid AS seq,
       r.state,
       r.plan_type,
       r.plan_origin,
       r.estimate_usd,
       r.created_at,
       r.updated_at,
       r.blocked_reason,
       CAST((julianday('now') - julianday(r.created_at)) * 86400 AS INTEGER) AS age_seconds,
       CASE WHEN r.state = 'approved' THEN 1 ELSE 0 END AS approved_unclaimed,
       CASE WHEN r.state = 'landed_with_errors' THEN 1 ELSE 0 END AS had_errors,
       json_extract(r.intent_json, '$.max_usd') AS max_usd,
       CASE WHEN r.blocked_reason = 'awaiting_plan' THEN 1 ELSE 0 END AS merge_can_clear,
       s.condition AS stuck_condition,
       s.detail AS stuck_detail
  FROM requests r
  LEFT JOIN (
        SELECT request_id,
               MIN(condition) AS condition,
               MIN(detail) AS detail
          FROM v_stuck
         GROUP BY request_id
       ) s ON s.request_id = r.id;
