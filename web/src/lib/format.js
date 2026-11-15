/**
 * Presentation helpers, not semantics.
 *
 * `stateTone` maps a state to a pill tone, and it lives here rather than in
 * Python because it is a colour choice. The *facet* -- which row is interesting
 * and why -- is decided server-side (`_with_facet`), because that precedence is
 * the read-model's opinion, not the view's.
 */

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

/** `$0.0412`, or an em dash. A price that rounds to `$0.04` hides the difference
 * between a plan that costs 4c and one that costs 4.9c, which is the whole
 * reason the column exists. */
export function usd(value) {
  if (value === null || value === undefined) return "—";
  return `$${Number(value).toFixed(4)}`;
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

/** The one sentence under a state badge: why this row is worth looking at.
 * Mirrors the precedence in `_with_facet` -- a stuck condition outranks the
 * state it is stuck in, and `blocked` is waiting, not breakage. */
export function whyItMatters(row) {
  if (row.facet === "broken") {
    return {
      text: row.stuck_condition,
      tone: "bad",
      title: row.stuck_detail ?? "",
    };
  }
  if (row.facet === "waiting") {
    const cap =
      row.max_usd === null || row.max_usd === undefined
        ? ""
        : ` · $${Number(row.max_usd).toFixed(0)}`;
    return { text: `${row.blocked_reason ?? "blocked"}${cap}`, tone: "warn" };
  }
  if (row.facet === "todo") {
    return {
      text: row.approved_unclaimed ? "unclaimed" : "needs approval",
      tone: "info",
    };
  }
  if (row.facet === "done" && row.had_errors) {
    return { text: "landed with errors", tone: "warn" };
  }
  return null;
}
