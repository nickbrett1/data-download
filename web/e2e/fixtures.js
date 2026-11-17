/**
 * The two view-models, as FastAPI emits them.
 *
 * The split this file exists for: pytest owns the *contract* (does
 * `/ui/dashboard.json` attach the right facet to the right row, does a stuck
 * condition outrank the state it is stuck in), and Playwright owns the *render*.
 * So the render tests do not need Python, a database or a token -- they need the
 * JSON, and they can state it here.
 */

export const dashboard = {
  rows: [
    {
      id: "req_9f2a41c8",
      created_at: "2026-11-14T23:10:00Z",
      seq: 1,
      label: "databento_batch_download",
      vendor: "Databento",
      why: "a trades tape for the Oct expiry",
      state: "succeeded",
      plan_type: "databento_batch_download",
      estimate_usd: 0.0412,
      age_seconds: 3840,
      facet: "done",
      facet_label: "Finished",
      had_errors: false,
      stuck_condition: null,
      stuck_detail: null,
      blocked_reason: null,
      max_usd: null,
      approved_unclaimed: false,
    },
    {
      id: "req_4c81be07",
      created_at: "2026-11-14T23:53:00Z",
      seq: 2,
      label: "databento_batch_download",
      vendor: "Databento",
      why: "the MBO window the resolver priced",
      state: "running",
      plan_type: "databento_batch_download",
      estimate_usd: 0.88,
      age_seconds: 125,
      // A stuck condition outranks the state it is stuck in: this row's state
      // is `running`, and the server still files it under `broken`.
      facet: "broken",
      facet_label: "Broken",
      had_errors: false,
      stuck_condition: "lease_expired",
      stuck_detail: "the claimant stopped heartbeating",
      blocked_reason: null,
      max_usd: null,
      approved_unclaimed: false,
    },
    {
      id: "req_b31d5e0a",
      created_at: "2026-11-14T21:40:00Z",
      seq: 3,
      label: "databento_batch_download",
      vendor: "Databento",
      why: "one day of quotes for ES",
      state: "approved",
      plan_type: "databento_batch_download",
      estimate_usd: 1.5,
      age_seconds: 7200,
      facet: "todo",
      facet_label: "Approval",
      had_errors: false,
      stuck_condition: null,
      stuck_detail: null,
      blocked_reason: null,
      max_usd: null,
      approved_unclaimed: true,
    },
    {
      id: "req_0a7c9d33",
      created_at: "2026-11-13T09:00:00Z",
      seq: 4,
      label: "OPRA.PILLAR · trades · 2026-10-01→2026-10-08",
      vendor: "Databento",
      why: "Oct expiry cycle",
      state: "blocked",
      plan_type: null,
      estimate_usd: 2,
      age_seconds: 90000,
      // One blocked row used to be one label. It is now three, because the
      // next move has three different owners: the caller adds the params, or
      // someone decides about code, or someone has already been asked.
      facet: "needs_params",
      facet_label: "Needs parameters",
      match_verdict: "needs_params",
      escalated_at: null,
      gap: {
        verdict: "needs_params",
        dataset: "OPRA.PILLAR",
        plan_type: "databento_batch_download",
        missing: ["symbols", "stype_in"],
        rejected: [],
      },
      had_errors: false,
      stuck_condition: null,
      stuck_detail: null,
      blocked_reason: "awaiting_plan",
      max_usd: 25,
      approved_unclaimed: false,
    },
    {
      id: "req_e55f10b2",
      created_at: "2026-11-12T15:20:00Z",
      seq: 5,
      label: "databento_batch_download",
      vendor: "Databento",
      why: "backfill for the lake",
      state: "landed_with_errors",
      plan_type: "databento_batch_download",
      estimate_usd: 0.5,
      age_seconds: 200000,
      facet: "done",
      facet_label: "Finished",
      had_errors: true,
      stuck_condition: null,
      stuck_detail: null,
      blocked_reason: null,
      max_usd: null,
      approved_unclaimed: false,
    },
  ],
  total: 5,
  facets: {
    broken: [],
    needs_params: [],
    no_plan: [],
    requested: [],
    todo: [],
    working: [],
    done: [],
    other: [],
  },
  facet_order: [
    "broken",
    "needs_params",
    "no_plan",
    "requested",
    "todo",
    "working",
    "done",
  ],
  facet_labels: {},
  showing: "all",
};

// The counts are derived rather than hand-written: a fixture whose facet counts
// disagree with its rows cannot tell a bug in the page from a bug in the fixture.
for (const row of dashboard.rows) {
  dashboard.facets[row.facet].push(row);
}

// The book before anything has been filed: the state the live service is in on
// a fresh install, and the one with the least to say.
export const emptyBook = {
  ...dashboard,
  rows: [],
  total: 0,
  facets: {
    broken: [],
    needs_params: [],
    no_plan: [],
    requested: [],
    todo: [],
    working: [],
    done: [],
    other: [],
  },
};

export const planTypes = {
  types: [
    {
      name: "databento_batch_download",
      version: 3,
      vendor: "Databento",
      anchor: "databento_batch_download",
      description:
        "One batch download from Databento, priced before it is approved.",
      schema: {
        required_count: 2,
        optional_count: 1,
        raw: '{\n  "$schema": "https://json-schema.org/draft/2020-12/schema"\n}',
        fields: [
          {
            name: "dataset",
            type: "string",
            required: true,
            description: "The Databento dataset id, e.g. GLBX.MDP3.",
            constraints: ["enum: GLBX.MDP3, XNAS.ITCH", "minLength: 1"],
          },
          {
            name: "start",
            type: "string",
            required: true,
            description: "First day to fetch, inclusive.",
            constraints: ["format: date"],
          },
          {
            name: "symbols",
            type: "array",
            required: false,
            description: "",
            constraints: [],
          },
        ],
      },
    },
    {
      name: "http_fetch",
      version: 1,
      anchor: "http_fetch",
      description: "A single URL, fetched once.",
      schema: {
        required_count: 1,
        optional_count: 0,
        raw: "{}",
        fields: [
          {
            name: "url",
            type: "string",
            required: true,
            description: "Absolute http(s) URL.",
            constraints: ["format: uri"],
          },
        ],
      },
    },
  ],
};

// The page behind a row: what `/ui/request/<id>.json` answers. Kept beside the
// book because the detail view is not a second read-model — it is one row of
// the book plus the three things the list deliberately drops.
export const requestDetail = {
  ...dashboard.rows[3],
  params_json: undefined,
  payload: {
    body_kind: "intent",
    plan_type: null,
    params: {
      dataset: "OPRA.PILLAR",
      schema: "trades",
      window: { start: "2026-10-01", end: "2026-10-08" },
    },
    intent: {
      kind: "intent",
      intent: {
        dataset: "OPRA.PILLAR",
        schema: "trades",
        window: { start: "2026-10-01", end: "2026-10-08" },
      },
      max_usd: 25,
      why: "OPRA.PILLAR trades for the Oct expiry cycle",
    },
    plan: null,
    max_usd: 25,
    why: "OPRA.PILLAR trades for the Oct expiry cycle",
  },
  // Phase 2.5: the report behind the badge. `match` is present only on rows
  // stored after the match existed -- a row filed before it has no verdict, and
  // inventing one at read time would date it to the read.
  match: {
    verdict: "needs_params",
    dataset: "OPRA.PILLAR",
    registry: "b3d1f0c9a2e47861",
    plan_type: null,
    candidates: [
      {
        plan_type: "databento_batch_download",
        version: 1,
        vendor: "Databento",
        verdict: "needs_params",
        detail: "missing required params",
        missing: ["symbols", "stype_in"],
        rejected: {},
        params: {
          dataset: "OPRA.PILLAR",
          schema: "trades",
          start: "2026-10-01",
          end: "2026-10-08",
        },
      },
    ],
    would_fit: {
      plan_type: "databento_batch_download",
      missing: ["symbols", "stype_in"],
      params: {
        dataset: "OPRA.PILLAR",
        schema: "trades",
        start: "2026-10-01",
        end: "2026-10-08",
        symbols: ["<symbols>"],
        stype_in: "<stype_in>",
      },
      note: "an example shape, not a quote: the missing values are placeholders",
    },
  },
  identity: {
    job_hash: null,
    code_ref: null,
    landed_sha256: null,
    errors: [],
  },
  events: [
    {
      id: 1,
      ts: "2026-11-13T09:01:00Z",
      actor_kind: "submitter",
      event: "submitted",
      detail: { body_kind: "intent", blocked_reason: "awaiting_plan" },
    },
  ],
};

// The three blocked answers side by side, which is the whole point of the
// split: the same `state`, three different owners of the next move. Built from
// the one blocked row above so nothing drifts but the field under test.
function blockedRow(seq, state) {
  const base = dashboard.rows[3];
  return { ...base, seq, ...state };
}

// The page behind a `draft`: a bound Job, a ceiling, and one thing the reader
// can do about it. `match` is null because a bound row has nothing to explain --
// the verdict already did its work.
export const draftDetail = {
  ...requestDetail,
  id: "req_draft01",
  seq: 6,
  label: "databento_batch_download",
  state: "draft",
  facet: "todo",
  facet_label: "Approval",
  blocked_reason: null,
  max_usd: 5,
  why: "test row: futures trades, ES continuous, two days",
  payload: {
    body_kind: "plan",
    plan_type: "databento_batch_download",
    params: {
      dataset: "GLBX.MDP3",
      schema: "trades",
      symbols: ["ES.c.0"],
      stype_in: "continuous",
      start: "2026-11-04",
      end: "2026-11-06",
    },
    intent: null,
    plan: null,
    max_usd: 5,
    why: "test row: futures trades, ES continuous, two days",
  },
  match: null,
  identity: {
    job_hash:
      "a3d57f9bf1dc2af76782b6b0fecd8cf701d1575742aa9cd5ce84814b8763df1e",
    code_ref: "07bda478",
    landed_sha256: null,
    errors: [],
  },
  events: [
    {
      id: 1,
      ts: "2026-11-15T23:42:33Z",
      actor_kind: "submitter",
      event: "submitted",
      detail: { body_kind: "plan", plan_type: "databento_batch_download" },
    },
  ],
};

export const blockedBook = (() => {
  const rows = [
    blockedRow(1, {}),
    blockedRow(2, {
      id: "req_77a1c204",
      facet: "no_plan",
      facet_label: "No plan yet",
      match_verdict: "not_claimed",
      escalated_at: null,
      gap: {
        verdict: "not_claimed",
        dataset: "ZZZZ.UNKNOWN",
        plan_type: null,
        missing: [],
        rejected: [],
      },
    }),
    blockedRow(3, {
      id: "req_55c9e18b",
      facet: "requested",
      facet_label: "Plan requested",
      match_verdict: "not_claimed",
      escalated_at: "2026-11-14T08:15:00Z",
      gap: {
        verdict: "not_claimed",
        dataset: "ZZZZ.UNKNOWN",
        plan_type: null,
        missing: [],
        rejected: [],
      },
    }),
  ];
  const facets = {
    broken: [],
    needs_params: [],
    no_plan: [],
    requested: [],
    todo: [],
    working: [],
    done: [],
    other: [],
  };
  for (const row of rows) facets[row.facet].push(row);
  return {
    ...dashboard,
    rows,
    total: rows.length,
    facets,
  };
})();
