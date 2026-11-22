-- Phase 1 request store, phase 2 shapes.
--
-- Deviations from `data-download-interface-and-operations` §8.1 are deliberate
-- and limited to what a `blocked` intent row needs (§7.9.2): `job_json` and
-- `estimate_usd` are nullable, with the invariant stated as a CHECK instead of
-- NOT NULL. `estimate_usd` is always NULL this phase because nothing prices
-- anything.
--
-- Columns §8.1 carries that this phase does not need yet (`actual_usd`,
-- `sdk_version`, `blocked_since`, `author_attempts`) are not columns here; they
-- arrive with the phases that use them.
--
-- `why` and `max_usd` *are* columns, because §7.2 requires both on every submit
-- and phase 1 was reading them out of `intent_json` only. That meant a plan
-- body lost both silently: `validate_plan` returns the plan, and the projection
-- kept nothing the caller said about it. The columns are nullable for rows
-- written before this change, and `v_requests` falls back to `intent_json` so
-- those rows still show their ceiling.

CREATE TABLE IF NOT EXISTS requests (
  id               TEXT PRIMARY KEY,
  state            TEXT NOT NULL,          -- draft|blocked|approved|running|succeeded|landed_with_errors|failed
  body_kind        TEXT NOT NULL,          -- plan|intent
  plan_type        TEXT,                   -- NULL when body_kind='intent'
  job_json         TEXT,                   -- the bound Job (§2); NULL iff state='blocked'
  params_json      TEXT NOT NULL,
  payload_hash     TEXT NOT NULL,          -- hash of the whole body; indexed, not UNIQUE
  idempotency_key  TEXT,                   -- caller's opt-in retry token (§5.6)
  intent_json      TEXT,                   -- NULL when body_kind='plan'
  estimate_usd     REAL,                   -- NULL iff state='blocked'; nothing populates it this phase
  blocked_reason   TEXT,                   -- NULL unless state='blocked'
  plan_origin      TEXT,                   -- profile|resolved|adhoc
  matched_profile  TEXT,
  code_ref         TEXT,                   -- git sha; phase 3
  job_hash         TEXT,                   -- derived: sha256(canonical(job_json))
  owner            TEXT,                   -- the claimant: host:pid:uuid (§8.2)
  lease_until      TEXT,                   -- phase 3
  landed_sha256    TEXT,                   -- phase 5
  errors_json      TEXT,
  match_verdict    TEXT,                   -- phase 2.5: matched|needs_params|cannot_express|not_claimed
  match_report_json TEXT,                  -- the whole MatchReport, with a registry digest
  escalated_at     TEXT,                   -- phase 2.5: when a caller asked for a new plan
  why              TEXT,                   -- the human reason, verbatim (§7.2)
  max_usd          REAL,                   -- the ceiling that travelled with the body
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  CHECK (state IN ('draft','blocked','approved','running','succeeded','landed_with_errors','failed')),
  CHECK (body_kind IN ('plan','intent')),
  CHECK ((state = 'blocked') = (job_json IS NULL)),
  -- A repeated body is a second Request, not a duplicate to refuse (§5.6). The
  -- same payload_hash filed twice therefore has to be *allowed* here; the
  -- exactly-once path is the caller's Idempotency-Key, which stays unique.
  CHECK (idempotency_key IS NULL OR length(idempotency_key) > 0),
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
  -- `submitter` is the API caller (phase 1), `human` is the CLI approver
  -- (phase 3), `runner` is the claim. §8.1 prints three names (`human`/`agent`/
  -- `runner`); the record needed a fourth for "an API caller" and keeping it
  -- says less than calling every submit `human` would wrongly claim.
  actor_kind   TEXT NOT NULL CHECK (actor_kind IN ('submitter','human','agent','runner','system')),
  event        TEXT NOT NULL,
  detail_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_state       ON requests(state);
CREATE INDEX IF NOT EXISTS idx_requests_plan_type   ON requests(plan_type);
-- Lookup, not refusal. Two Requests for the same thing are two artefacts in two
-- directories, and the service does not police what a user already holds.
CREATE INDEX IF NOT EXISTS idx_requests_payload_hash ON requests(payload_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_idempotency
    ON requests(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_request_id    ON events(request_id, id);

-- ---------------------------------------------------------------------------
-- Read surface (§7.8). The dashboard issues only SELECTs against these views.
-- ---------------------------------------------------------------------------

-- What is waiting on me. Since phase 3 an `approved` row has `owner IS NULL`
-- by construction -- `claim` moves it to `running`, `release` clears both owner
-- and lease -- so `approved_unclaimed` is exact rather than assumed.
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

-- What is broken. Exactly three conditions, all breakage (§7.8.1):
--   1. lease_expired      - claimed, lease ran out, the claimant died mid-run
--   2. running_too_long   - running with a valid lease but no progress for >6h
--   3. repeat_failure     - one JOB_HASH has >=3 failed rows
--
-- "Approved, and nobody has picked it up" used to be a fourth, behind a 900 s
-- claim window measured from `created_at` -- the filing, not the approval. A
-- request is normally filed, left waiting, and approved some time later, so the
-- window had always elapsed before anyone looked: every approval was instantly
-- reported as stuck, with the detail "older than the claim window" printed one
-- second after approving it. There is no window worth having, because there is
-- nothing to wait for -- an approved row is unclaimed from the moment it is
-- approved until a claim moves it to `running`, and `v_pending.approved_unclaimed`
-- already says so, under "Approval". It is also not breakage: money authorised
-- and waiting for a worker is a queue, not a fault. So it is gone from here
-- rather than made instant, which would have painted every approval red.
--
-- Condition 3 groups on `job_hash`, not `payload_hash` as §7.8.1 prints it.
-- As written it was unreachable (payload_hash was UNIQUE, so a second row with
-- the same value could not exist) and, now that §5.6 drops that constraint, it
-- would be reachable but *wrong*: identical content is evidence of two Requests,
-- not of a broken Plan. A re-filed request with a different `why` has a
-- different payload_hash but the same job_hash, and since retries are Attempts
-- inside one Request, three rows sharing a job_hash are three deliberate filings
-- of a Job that keeps failing - which is the binder or the Job. Recorded in the
-- phase-1 Result as a design finding, renamed by phase 2.
-- `blocked` is deliberately NOT here: waiting on a plan is not breakage
-- (§7.9.4), it is `v_blocked`.
--
-- Three conditions, and thresholds as literals because there is nowhere else to
-- put them this phase: 6 h no-progress, 3 failed rows.
-- Dropped and recreated like v_requests: phase 2 migrates `plan_hash` to
-- `job_hash`, and CREATE VIEW IF NOT EXISTS would leave a deployed store on a
-- definition that selects a column the migration removed.
DROP VIEW IF EXISTS v_stuck;
CREATE VIEW v_stuck AS
SELECT request_id, state, condition, age_seconds, detail FROM (
  -- The aliases live on the first branch only, as they always have: the outer
  -- SELECT names the union's columns from here.
  SELECT id AS request_id,
         state,
         'lease_expired' AS condition,
         CAST((julianday('now') - julianday(lease_until)) * 86400 AS INTEGER) AS age_seconds,
         'claimed, lease expired - the claimant died mid-run' AS detail
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
         'job_hash failed ' || g.cnt || ' times - this Job never works'
    FROM requests r
    JOIN (SELECT job_hash, COUNT(*) AS cnt, MAX(created_at) AS latest
            FROM requests
           WHERE state = 'failed'
             AND job_hash IS NOT NULL
           GROUP BY job_hash
          HAVING COUNT(*) >= 3) g
      ON r.job_hash = g.job_hash
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
--
-- Dropped and recreated on every start, unlike the rest of this file. A view is
-- a query, not data, and `CREATE VIEW IF NOT EXISTS` would leave a database
-- created yesterday on yesterday's columns -- which is exactly how the new
-- `why`/`params_json` columns would go missing on the one deployment that has
-- rows in it.
DROP VIEW IF EXISTS v_requests;
CREATE VIEW v_requests AS
SELECT r.id,
       r.rowid AS seq,
       r.state,
       r.body_kind,
       r.plan_type,
       r.plan_origin,
       r.estimate_usd,
       r.created_at,
       r.updated_at,
       r.blocked_reason,
       CAST((julianday('now') - julianday(r.created_at)) * 86400 AS INTEGER) AS age_seconds,
       CASE WHEN r.state = 'approved' THEN 1 ELSE 0 END AS approved_unclaimed,
       CASE WHEN r.state = 'landed_with_errors' THEN 1 ELSE 0 END AS had_errors,
       -- One ceiling, from two places: the column this phase added, and the
       -- verbatim intent body that older rows still carry it in.
       COALESCE(r.max_usd, json_extract(r.intent_json, '$.max_usd')) AS max_usd,
       -- Same fallback for the reason: rows written before `why` was a column
       -- carry it in the verbatim intent body, and losing it would make the
       -- detail page claim no reason was given by a caller who gave one.
       COALESCE(r.why, json_extract(r.intent_json, '$.why')) AS why,
       r.params_json AS params_json,
       -- Phase 2.5: why an intent has no Job, and whether a new plan was asked
       -- for. In the union view rather than in each of the four facet views,
       -- because those are `IF NOT EXISTS` and a deployed store would keep the
       -- old column list forever (see the note above).
       r.match_verdict,
       r.match_report_json,
       r.escalated_at,
       CASE WHEN r.blocked_reason = 'awaiting_plan' THEN 1 ELSE 0 END AS merge_can_clear,
       -- Why a settled row did not land cleanly. It was written by the runner
       -- from phase 3 on and read by nothing: a `failed` row showed the badge
       -- and no reason, which is the one thing a reader opening it wants. The
       -- list carries only the code it needs for the half-line under the badge
       -- (`_book_row` reduces it); the page carries the sentence too.
       r.errors_json AS errors_json,
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
