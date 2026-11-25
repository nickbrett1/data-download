/**
 * The two view-models, as FastAPI emits them.
 *
 * The split this file exists for: pytest owns the *contract* (does
 * `/ui/dashboard.json` attach the right facet to the right row, does a stuck
 * condition outrank the state it is stuck in), and Playwright owns the *render*.
 * So the render tests do not need Python, a database or a token -- they need the
 * JSON, and they can state it here.
 */

// What the vendor says about a dataset code, as the three read routes now carry
// it (`/ui/dashboard.json`, `/ui/request/<id>.json`, `/ui/datasets.json`).
// `label` is the server's: it is the vendor's description, or the sentence that
// says the vendor has none -- the page never decides which.
export const glbx = {
  vendor: "Databento",
  dataset: "GLBX.MDP3",
  described: true,
  description: "CME Globex MDP 3.0",
  label: "CME Globex MDP 3.0",
  venue: "GLBX",
  venues: ["GLBX"],
  available_from: "2010-06-06",
  available_to: "now",
  schemas: ["ohlcv-1d", "trades"],
  fetched_at: "2026-11-23T03:00:00Z",
};

export const dashboard = {
  rows: [
    {
      id: "req_9f2a41c8",
      // The dataset, and what the vendor calls it: a code alone is vocabulary
      // only Databento has memorised.
      dataset: "GLBX.MDP3",
      catalogue: glbx,
      created_at: "2026-11-14T23:10:00Z",
      seq: 1,
      label: "market_data_window",
      vendor: "Databento",
      why: "a trades tape for the Oct expiry",
      state: "succeeded",
      window: { start: "2026-10-01", end: "2026-10-08", days: 7 },
      plan_type: "market_data_window",
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
      label: "market_data_window",
      vendor: "Databento",
      why: "the MBO window the resolver priced",
      state: "running",
      window: { start: "2026-11-14", end: "2026-11-15", days: 1 },
      plan_type: "market_data_window",
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
      label: "market_data_window",
      vendor: "Databento",
      why: "one day of quotes for ES",
      state: "approved",
      window: { start: "2026-11-15", end: "2026-11-16", days: 1 },
      plan_type: "market_data_window",
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
      window: { start: "2026-10-01", end: "2026-10-08", days: 7 },
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
        plan_type: "market_data_window",
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
      label: "market_data_window",
      vendor: "Databento",
      why: "backfill for the lake",
      state: "landed_with_errors",
      window: { start: "2026-11-01", end: "2026-11-05", days: 4 },
      plan_type: "market_data_window",
      estimate_usd: 0.5,
      age_seconds: 200000,
      facet: "done",
      facet_label: "Finished",
      had_errors: true,
      failure: {
        code: "verify_shortfall",
        detail: "the vendor sent 3 of the 5 files the manifest claimed",
      },
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
  // Whether anyone could ask the runtime what is installed -- `known: false` is
  // "unknown", which is not the same answer as "nothing installed".
  drivers: {
    known: true,
    detail: "",
    count: 2,
    // Where a transcript is readable, when a *browser* can reach this
    // deployment's canvas: the canvas root, not the API address the app calls
    // (`http://openhands:8000` resolves only inside the app's own network, and
    // a link built from it is dead in the reader's browser). Empty would mean
    // the id is shown but is not a link.
    canvas_url: "http://canvas.local:3088/canvas",
    // The driver repository and its base branch: a row links to the code, so
    // the payload says where the code is rather than the view guessing a host.
    repo: "nickbrett1/data-download-drivers",
    base: "main",
    // The browse list: every artefact, joined to the types it answers. `stub`
    // answers nothing, which is a state the page has to draw rather than hide.
    installed: [
      {
        name: "databento",
        version: "1",
        vendor: "Databento",
        answers: ["market_data_window"],
      },
      {
        name: "stub",
        version: "1",
        vendor: "Stub",
        answers: [],
      },
    ],
  },
  types: [
    {
      name: "market_data_window",
      version: 3,
      // The generic shape: no pinned seller, an attribution table instead.
      vendor: "",
      vendors: { Databento: ["*.ITCH", "*.MDP3", "EQUS.*"] },
      // The second half of the model: who answers the question. A seller with
      // an artefact is a runnable implementation; one without is a type that
      // can be read and cannot yet be run.
      implementations: [
        {
          vendor: "Databento",
          datasets: ["*.ITCH", "*.MDP3", "EQUS.*"],
          driver: {
            name: "databento",
            version: "1",
            // The artefact's directory in the driver repo, which is where a
            // reader goes to see the code.
            path: "drivers/databento/1",
          },
          // The authoring request that produced it: the loop's record of what
          // was asked for, which is a different fact from what is installed.
          build: {
            vendor: "Databento",
            state: "installed",
            open: false,
            attempts: 1,
            branch: "driver-loop/databento-20261124t160220z",
            commit: "9f28f7583a9b233802aa6d32df4b4dc6a563b1c7",
            // The authoring conversation. The fixture deployment has a canvas,
            // so the page can turn this id into a link.
            author_ref: "a31b725e-c0ee-4e27-abfa-38dc921e83c8",
            error: "",
            created_at: "2026-11-24T16:02:07Z",
            updated_at: "2026-11-24T16:37:29Z",
          },
          installed: true,
        },
      ],
      installed_count: 1,
      anchor: "market_data_window",
      description:
        "A vendor job for a symbol-scoped window of records, split per day and verified against the vendor's own manifest.",
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
      // A pinned single-seller type, so the page's two branches are both drawn.
      name: "http_fetch",
      version: 1,
      vendor: "Nabu",
      vendors: {},
      implementations: [
        {
          vendor: "Nabu",
          datasets: [],
          driver: null,
          // Queued: someone asked for a driver for this vendor and the loop has
          // not finished one. The row must say that, not "no driver installed".
          build: {
            vendor: "Nabu",
            state: "building",
            open: true,
            attempts: 1,
            branch: "driver-loop/nabu-20261124t161000z",
            commit: "",
            author_ref: "",
            error: "",
            created_at: "2026-11-24T16:10:00Z",
            updated_at: "2026-11-24T16:12:00Z",
          },
          installed: false,
        },
      ],
      installed_count: 0,
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
        plan_type: "market_data_window",
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
      plan_type: "market_data_window",
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
  failure: null,
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
  label: "market_data_window",
  dataset: "GLBX.MDP3",
  catalogue: glbx,
  state: "draft",
  facet: "todo",
  facet_label: "Approval",
  blocked_reason: null,
  max_usd: 5,
  // Phase 4: the vendor's quote for exactly this Job, four decimals, because
  // 4c and 4.9c are different purchases.
  estimate_usd: 0.8115,
  why: "test row: futures trades, ES continuous, two days",
  payload: {
    body_kind: "plan",
    plan_type: "market_data_window",
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
  failure: null,
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
      detail: { body_kind: "plan", plan_type: "market_data_window" },
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

// What `/ui/datasets.json` answers: the vendor -> dataset -> request tree the
// browser draws. Two requests, because the page's hardest case is the
// difference between "finished" and "still arriving" sitting in one list.
export const datasets = {
  root: "/datasets",
  configured: true,
  vendors: [
    {
      vendor: "Databento",
      vendor_slug: "databento",
      requests: 2,
      files: 2,
      bytes: 8158,
      datasets: [
        {
          dataset: "GLBX.MDP3",
          catalogue: glbx,
          files: 2,
          bytes: 8158,
          requests: [
            {
              request_id: "req_landed01",
              state: "succeeded",
              plan_type: "market_data_window",
              vendor: "Databento",
              vendor_slug: "databento",
              dataset: "GLBX.MDP3",
              symbols: ["ES.c.0"],
              stype_in: "continuous",
              schema: "trades",
              start: "2026-11-04",
              end: "2026-11-06",
              estimate_usd: 0.8115,
              created_at: "2026-11-19T03:00:13Z",
              updated_at: "2026-11-20T13:27:24Z",
              landed_sha256:
                "c69889effb5db6e7eed6dca19ed257b07b1378762e1cbb3b8b93bd00e297ed57",
              verdict: "pass",
              disposition: "commit",
              driver: "StubDatabentoDriver",
              settled_at: "2026-11-20T13:27:24Z",
              on_disk: true,
              total_bytes: 8158,
              files: [
                {
                  name: "GLBX.MDP3-2026-11-04.dbn.zst",
                  bytes: 4079,
                  sha256:
                    "c1cb1e057aac8449456ef5ddf79b58140935d7fe0f8f720107fb47fdae45b4c0",
                  rows: 1000,
                  href: "/datasets/file/databento/GLBX.MDP3/req_landed01/GLBX.MDP3-2026-11-04.dbn.zst",
                },
                {
                  name: "GLBX.MDP3-2026-11-05.dbn.zst",
                  bytes: 4079,
                  sha256:
                    "a0fa9c27cea3d4b337e45a378bd909ec021d7352b0f8140e8176f57913d9c214",
                  rows: 1000,
                  href: "/datasets/file/databento/GLBX.MDP3/req_landed01/GLBX.MDP3-2026-11-05.dbn.zst",
                },
              ],
            },
            {
              request_id: "req_running01",
              state: "running",
              plan_type: "market_data_window",
              vendor: "Databento",
              vendor_slug: "databento",
              dataset: "GLBX.MDP3",
              symbols: ["NQ.c.0"],
              stype_in: "continuous",
              schema: "trades",
              start: "2026-11-04",
              end: "2026-11-08",
              estimate_usd: 1.9021,
              created_at: "2026-11-20T12:00:00Z",
              updated_at: "2026-11-20T13:30:00Z",
              landed_sha256: null,
              verdict: null,
              disposition: null,
              driver: null,
              settled_at: null,
              on_disk: true,
              total_bytes: 4079,
              files: [
                {
                  name: "GLBX.MDP3-2026-11-04.dbn.zst",
                  bytes: 4079,
                  sha256: null,
                  rows: null,
                  href: "/datasets/file/databento/GLBX.MDP3/req_running01/GLBX.MDP3-2026-11-04.dbn.zst",
                },
              ],
            },
          ],
        },
      ],
    },
  ],
  totals: {
    vendors: 1,
    datasets: 1,
    requests: 2,
    files: 3,
    bytes: 12237,
    in_flight: 1,
  },
};
