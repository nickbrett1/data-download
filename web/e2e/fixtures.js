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
      state: "blocked",
      plan_type: "databento_batch_download",
      estimate_usd: 2,
      age_seconds: 90000,
      facet: "waiting",
      facet_label: "Waiting on a plan",
      had_errors: false,
      stuck_condition: null,
      stuck_detail: null,
      blocked_reason: "waiting on a plan",
      max_usd: 25,
      approved_unclaimed: false,
    },
    {
      id: "req_e55f10b2",
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
