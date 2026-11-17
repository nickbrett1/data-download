/**
 * Presentation helpers, not semantics.
 *
 * `stateTone` maps a state to a pill tone, and it lives here rather than in
 * Python because it is a colour choice. The *facet* -- which row is interesting
 * and why -- is decided server-side (`_with_facet`), because that precedence is
 * the read-model's opinion, not the view's.
 */

/** The three facets that mean `state = blocked`. Not `waiting`: one word, three
 * owners of the next move, and the server decides which is which. */
const BLOCKED_FACETS = new Set(["needs_params", "no_plan", "requested"]);

const TONES = {
  draft: "",
  approved: "info",
  running: "info",
  succeeded: "ok",
  landed_with_errors: "warn",
  failed: "bad",
  blocked: "warn",
};

export function stateTone(state) {
  return TONES[state] ?? "";
}

/** The state, as a person reads it. The stored value is the API's word and
 * stays that way in the database, the CHECK constraint and the JSON; it is
 * only ever *shown* through this map.
 *
 * `draft` is the one that was actively misleading: everywhere else a draft is
 * unsubmitted and editable, and this row is submitted, immutable, bound to a
 * plan and priced -- it is waiting for someone to authorise the spend. So it
 * says `awaiting approval`, which also gives `approved` the partner it never
 * had. A word this map has never heard of renders as itself, because a state
 * added later should look odd, not vanish. */
const STATE_LABELS = {
  draft: "Awaiting approval",
  approved: "Approved",
  blocked: "Blocked",
  running: "Running",
  succeeded: "Succeeded",
  landed_with_errors: "Landed with errors",
  failed: "Failed",
};

export function stateLabel(state) {
  return STATE_LABELS[state] ?? state;
}

/** `$0.0412`, or an em dash. A price that rounds to `$0.04` hides the difference
 * between a plan that costs 4c and one that costs 4.9c, which is the whole
 * reason the column exists. */
export function usd(value) {
  if (value === null || value === undefined) return "—";
  return `$${Number(value).toFixed(4)}`;
}

/** A ceiling, not a price: `$5`, `$5.50`, `$120`.
 *
 * Four decimals belong on a quote -- 4c and 4.9c are different purchases -- and
 * nowhere near the sentence "approve up to…", where nobody authorises
 * `$5.0000`. Same number, different question, so a second formatter. */
export function bound(value) {
  if (value === null || value === undefined) return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  return `$${amount.toFixed(2).replace(/\.00$/, "")}`;
}

/** When a request was filed, as a person reads a clock: `14 Nov 23:41`.
 * Local time on purpose -- the reader is the one who filed it, and the service
 * stores UTC (`Z`), so a raw timestamp would be an hour or two out in the very
 * column they use to remember when they asked. */
export function filed(iso) {
  if (!iso) return "—";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "—";
  const day = when.getDate();
  const month = when.toLocaleString("en-GB", { month: "short" });
  const hh = String(when.getHours()).padStart(2, "0");
  const mm = String(when.getMinutes()).padStart(2, "0");
  return `${day} ${month} ${hh}:${mm}`;
}

export function age(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total)) return "—";
  if (total < 60) return `${Math.floor(total)}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m`;
  if (total < 86400) {
    return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  }
  return `${Math.floor(total / 86400)}d ${Math.floor((total % 86400) / 3600)}h`;
}

/** The ceiling, appended to whatever the badge says: the answer's price is
 * part of the answer, and the row has no column for it (see Dashboard). */
function withCap(text, row) {
  if (row.max_usd === null || row.max_usd === undefined) return text;
  return `${text} · $${Number(row.max_usd).toFixed(0)}`;
}

/** A blocked row says whose move it is.
 *
 * `needs_params` is something the caller can act on this minute ("add symbols,
 * stype_in"); `no_plan` is a decision about code; `requested` is that decision
 * already asked for. Three badges, because they were one badge -- "waiting on a
 * plan" -- and that one word hid the only question a reader has: what do I do?
 */
function blockedMove(row) {
  const gap = row.gap ?? {};
  if (row.facet === "needs_params") {
    const missing = gap.missing ?? [];
    const text = missing.length
      ? `add ${missing.join(", ")}`
      : "add missing params";
    return {
      text: withCap(text, row),
      tone: "info",
      title: gap.plan_type
        ? `${gap.plan_type} already takes this dataset; these params are what it is short of`
        : "",
    };
  }
  if (row.facet === "no_plan") {
    // A rejected value first: "you sent something wrong" explains the row
    // better than "we have nothing", and it is not fixable by more fields.
    if ((gap.rejected ?? []).length) {
      return {
        text: withCap(`rejects ${gap.rejected.join(", ")}`, row),
        tone: "warn",
        title: gap.plan_type
          ? `${gap.plan_type} rejected a value this request sent`
          : "",
      };
    }
    const text = gap.dataset ? `no plan for ${gap.dataset}` : "no plan yet";
    return { text: withCap(text, row), tone: "warn" };
  }
  return {
    text: withCap("new plan requested", row),
    tone: "info",
    title: row.escalated_at ? `asked for on ${filed(row.escalated_at)}` : "",
  };
}

/** The one sentence under a state badge: why this row is worth looking at.
 * Mirrors the precedence in `_with_facet` -- a stuck condition outranks the
 * state it is stuck in, and `blocked` is split by who moves next, not by the
 * state it is in. */
export function whyItMatters(row) {
  if (row.facet === "broken") {
    return {
      text: row.stuck_condition,
      tone: "bad",
      title: row.stuck_detail ?? "",
    };
  }
  if (BLOCKED_FACETS.has(row.facet)) return blockedMove(row);
  if (row.facet === "todo") {
    // The state pill already carries the verb for a draft -- "awaiting
    // approval" -- so repeating it here was one fact in two spellings. This
    // column is for what the state does *not* say: what the approval would
    // authorise. `unclaimed` stays, because an approved row nobody picked up
    // is an anomaly, not a restatement of `approved`.
    if (row.approved_unclaimed) {
      return {
        text: "unclaimed",
        tone: "info",
        title: "approved, but no worker has claimed it",
      };
    }
    const cap = bound(row.max_usd);
    if (cap === "—") return null;
    return {
      text: `${cap} ceiling`,
      tone: "info",
      title: "approving authorises spend up to this ceiling",
    };
  }
  if (row.facet === "done" && row.had_errors) {
    return { text: "landed with errors", tone: "warn" };
  }
  return null;
}
