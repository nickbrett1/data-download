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
      facet_label: "To approve",
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
      facet: "waiting",
      facet_label: "Waiting on a plan",
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
    waiting: [],
    todo: [],
    working: [],
    done: [],
    other: [],
  },
  facet_order: ["broken", "waiting", "todo", "working", "done"],
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
    waiting: [],
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
  identity: {
    plan_hash: null,
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
