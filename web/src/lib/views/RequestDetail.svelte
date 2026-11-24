<script>
  /**
   * One request, in full.
   *
   * The list is a list of keys -- `#7`, what was asked for, and a badge for why
   * it is interesting. This is the answer to clicking one: the human `why`, the
   * body the caller actually sent, the identity columns, and the append-only
   * trail.
   *
   * Reading takes no credential and changes nothing. The page has exactly one
   * verb -- approve, on a `draft` row -- and it is not free: it needs the
   * `approve` token, which no agent holds and which a bare token never gets.
   *
   * The id is shown in full and in mono, because this is the page where you copy
   * it into a curl. Everywhere else it is a tooltip, because a key is not a name.
   */
  import Actions from "$lib/components/Actions.svelte";
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DetailPanel from "$lib/components/nb/DetailPanel.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView } from "$lib/api";
  import {
    bound,
    filed,
    isArchivable,
    isRetryable,
    stateLabel,
    stateTone,
    usd,
    windowOf,
  } from "$lib/format";

  // The three blocked facets are the same state and three different answers to
  // "what next", so the meta row asks the question the label answers.
  const BLOCKED = new Set(["needs_params", "no_plan", "requested"]);

  const TONES = {
    matched: "ok",
    needs_params: "info",
    cannot_express: "warn",
    not_claimed: "warn",
  };

  function show(value) {
    return typeof value === "string" ? value : JSON.stringify(value);
  }

  let { id } = $props();

  let data = $state(null);
  let failure = $state("");

  async function load(signal) {
    try {
      data = await getView(`/ui/request/${encodeURIComponent(id)}.json`, {
        signal,
      });
      failure = "";
    } catch (error) {
      if (error.name !== "AbortError") failure = error.message;
    }
  }

  $effect(() => {
    const controller = new AbortController();
    load(controller.signal);
    return () => controller.abort();
  });

  /** Archiving takes the row off the book, so this page -- a decision record --
   * is the wrong place to still be: you put it away, you do not want to keep
   * reading it. `assign("/")` rather than `history.back()` because a request
   * page is a link you can arrive at from anywhere (a note, a chat, a
   * bookmark), and "back" then leaves the app instead of returning to the
   * list. The list is the destination either way. */
  function leaveToList() {
    window.location.assign("/");
  }

  // Any verb at all, so a running row does not get an empty line where the
  // buttons would have been.
  const hasVerbs = $derived(
    Boolean(data) &&
      (data.state === "draft" || isRetryable(data) || isArchivable(data)),
  );

  const meta = $derived.by(() => {
    if (!data) return [];
    const rows = [
      { label: "request id", value: data.id, isCode: true },
      { label: "state", value: stateLabel(data.state) },
    ];
    // The facet is worth naming on a blocked row and nowhere else. There it
    // answers a question the state cannot: which of the three kinds of stuck
    // this is, and therefore who moves next. On a draft it was "in the book:
    // Approval" -- a filter bucket restated under a pill that already said
    // *Awaiting approval*, which is a label inventing a thing to say.
    if (BLOCKED.has(data.facet)) {
      rows.push({ label: "waiting on", value: data.facet_label });
    }
    // Approved and nobody has taken it. The same words the book uses under the
    // badge, because a reader who clicked through from `no worker has it yet`
    // should find that sentence here, not a second vocabulary for it. It is no
    // longer a stuck condition -- money authorised and waiting for a worker is
    // a queue, not a fault -- so this is where it is stated in full.
    if (data.approved_unclaimed) {
      rows.push({
        label: "worker",
        value: "none — no worker has picked it up",
      });
    }
    if (data.blocked_reason) {
      rows.push({
        label: "a merge can clear it",
        value: data.merge_can_clear ? "yes" : "no — this needs a human",
      });
    }
    if (data.stuck_condition) {
      rows.push({
        label: "stuck",
        value: data.stuck_detail
          ? `${data.stuck_condition} — ${data.stuck_detail}`
          : data.stuck_condition,
      });
    }
    if (data.vendor) {
      rows.push({ label: "vendor", value: data.vendor });
    }
    // The ceiling and the estimate are not here: they are about the request
    // itself, not about the row's bookkeeping, so they sit with the request at
    // the top of the page where somebody deciding has them in view.
    //
    // Nor is the age. A clock a reader has to subtract from is the `filed` date
    // said twice, and the book already answers this: it prints the date and
    // calls the age what it is -- a question nobody asked.
    rows.push({ label: "filed", value: filed(data.created_at) });
    return rows;
  });

  // The order a person reads a Databento request in: what, which feed, what
  // instruments, then the window. Anything the plan type adds that this list
  // does not name follows in the order the caller sent it, so a new param is
  // shown rather than silently dropped.
  const PARAM_ORDER = [
    "dataset",
    "schema",
    "symbols",
    "stype_in",
    "window",
    "start",
    "end",
  ];

  /** A param, as a person would say it.
   *
   * The four shapes a request actually uses: a window is a range with an arrow
   * rather than another `{"start":…,"end":…}` blob, symbols are a list that
   * reads as one, and `null` says `—` instead of the word "null". */
  function say(key, value) {
    if (value === null || value === undefined) return "—";
    if (Array.isArray(value)) return value.length ? value.join(", ") : "—";
    if (typeof value === "object") {
      if (value.start && value.end) return windowOf(value.start, value.end);
      return JSON.stringify(value);
    }
    return String(value);
  }

  const asked = $derived.by(() => {
    if (!data) return [];
    const params = data.payload?.params ?? {};
    // An intent arrives with `window: {start, end}` and a plan with flat
    // `start`/`end`. One range, said one way, whichever body it came from --
    // otherwise the same request reads differently depending on which door it
    // came through, which is exactly the confusion this page exists to end.
    const ranged = !params.window && Boolean(params.start && params.end);
    const rows = [];
    for (const [key, value] of Object.entries(params)) {
      if (ranged && (key === "start" || key === "end")) continue;
      rows.push({ key, value: say(key, value) });
    }
    if (ranged) {
      rows.push({
        key: "window",
        value: windowOf(params.start, params.end),
      });
    }
    const rank = (key) => {
      const at = PARAM_ORDER.indexOf(key);
      return at === -1 ? PARAM_ORDER.length : at;
    };
    return rows.sort((a, b) => rank(a.key) - rank(b.key));
  });

  const hasPrice = $derived(
    data?.estimate_usd !== null && data?.estimate_usd !== undefined,
  );
  const hasCeiling = $derived(
    data?.max_usd !== null && data?.max_usd !== undefined,
  );

  // The money, as more rows in the same grid the parameters use: `quoted` and
  // `ceiling` are fields of this request exactly as `window` and `stype_in`
  // are, so they are set the same way -- a label in one column, the value in
  // the other. A right-aligned block of their own made them look like a
  // different kind of fact.
  const money = $derived.by(() => {
    if (!data) return [];
    const rows = [];
    if (hasPrice) rows.push({ key: "quoted", value: usd(data.estimate_usd) });
    if (hasCeiling) rows.push({ key: "ceiling", value: bound(data.max_usd) });
    return rows;
  });

  // No figure, and the reason said plainly. Three different absences, three
  // different sentences -- a dash would make an unpriced request look free.
  const unpriced = $derived(
    hasPrice
      ? ""
      : hasCeiling
        ? "not priced — there is no job to quote"
        : "not priced — filed before pricing existed",
  );

  // Which field of an event's detail is the actor, when the trail recorded one.
  const ACTOR_OF = { approved: "actor", claimed: "owner", released: "owner" };

  /** The doer, when the event does not already say who.

   * `submitted` used to be followed by `submitter` -- the trail printing the
   * same word twice and calling it a column. The kind only earns its place when
   * it says something the event name cannot: who approved it, which worker took
   * it. Where there is no identity to print, there is no third column.
   */
  function who(event) {
    const field = ACTOR_OF[event.event];
    if (!field) return "";
    return event.detail?.[field] ?? "";
  }

  const rawBody = $derived.by(() => {
    if (!data) return "";
    const body =
      data.payload?.intent ?? data.payload?.job ?? data.payload?.params;
    return body ? JSON.stringify(body, null, 2) : "";
  });

  // The whole match report, shaped for the page. `match` is null on rows stored
  // before phase 2.5 (a verdict invented now would be dated wrongly) and on
  // rows that have a Job, where there is nothing to explain.
  const match = $derived.by(() => {
    if (!data?.match) return null;
    const report = data.match;
    const would = report.would_fit;
    return {
      verdict: report.verdict,
      dataset: report.dataset,
      registry: report.registry,
      plan_type: report.plan_type,
      would: would
        ? {
            plan_type: would.plan_type,
            note: would.note,
            params: Object.entries(would.params ?? {}).map(([key, value]) => ({
              key,
              value: show(value),
            })),
          }
        : null,
      candidates: (report.candidates ?? []).map((candidate) => ({
        plan_type: candidate.plan_type,
        version: candidate.version,
        verdict: candidate.verdict,
        // One sentence per type: the fields it wanted, or the value it refused.
        // The validator's own message is kept for the refused case, because
        // "outside the enum" is the bit a caller cannot see from their body.
        reason: candidate.detail ?? "",
        missing: candidate.missing ?? [],
        rejected: Object.entries(candidate.rejected ?? {}).map(
          ([where, why]) => ({
            where,
            why,
          }),
        ),
      })),
    };
  });
</script>

<AppShell
  footer={false}
  title="data-download"
  nav={[
    { label: "Requests", href: "/", current: true },
    { label: "Datasets", href: "/datasets", current: false },
    { label: "Plan types", href: "/plan-types", current: false },
  ]}
>
  {#snippet mark()}
    <AppMark />
  {/snippet}

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the request…</p>
  {:else}
    <DetailPanel
      title={`#${data.seq} · ${data.label}`}
      {meta}
      pill={{ label: stateLabel(data.state), tone: stateTone(data.state) }}
    >
      <!-- Above the request, because a reader who opens a failed row is asking
           "why" before anything else. The code is the phrase the code owns, and
           the sentence under it is the runner's, verbatim: paraphrasing a
           driver's refusal is how a reason becomes a guess. -->
      {#if data.failure}
        <section class="block failed">
          <p class="micro">why it failed</p>
          <p class="failed-code"><code>{data.failure.code}</code></p>
          {#if data.failure.detail}
            <p class="failed-detail">{data.failure.detail}</p>
          {/if}
        </section>
      {/if}

      <!-- The thing being decided about, before the verbs that decide it: a
           person reads what was asked for and why, then chooses. -->
      <section class="block request">
        <p class="micro">what was asked for</p>
        {#if asked.length || money.length}
          <dl class="kv spec">
            {#each asked as item (item.key)}
              <dt>{item.key}</dt>
              <dd><code>{item.value}</code></dd>
            {/each}
            <!-- The money, in the same breath as the thing it prices: the quote
                 is what a decision needs and the ceiling is what it authorises.
                 Same two columns as the parameters above. -->
            {#each money as item (item.key)}
              <dt>{item.key}</dt>
              <dd><code>{item.value}</code></dd>
            {/each}
          </dl>
        {:else}
          <p class="quiet">No parameters were recorded on this request.</p>
        {/if}
        {#if unpriced}
          <p class="quiet unpriced">{unpriced}</p>
        {/if}
      </section>

      <section class="block">
        <p class="micro">why</p>
        {#if data.payload?.why}
          <p class="why">{data.payload.why}</p>
        {:else}
          <p class="quiet">No reason was recorded.</p>
        {/if}
      </section>

      <!-- The verbs follow the evidence. They used to lead the page, on the
           argument that a reader opens it to *do* something -- and that put
           Approve above the parameters and the sentence saying why the request
           exists, so the decision was asked for before the question was read.
           A request page is a decision record: what was asked, why, and then
           one cluster of buttons. -->
      {#if hasVerbs}
        <Actions
          request={data}
          onchanged={() => load()}
          onarchived={leaveToList}
        />
      {/if}

      {#if BLOCKED.has(data.facet)}
        <section class="block">
          <p class="micro">why there is no job</p>
          {#if !match}
            <p class="quiet">
              This request was filed before the match report was stored, so
              there is no verdict for it. The registry has moved since; asking
              again is the only honest way to get one.
            </p>
          {:else}
            <dl class="kv">
              <dt>verdict</dt>
              <dd>
                <span class="pill {TONES[match.verdict] ?? ''}"
                  >{match.verdict}</span
                >
              </dd>
              {#if match.dataset}
                <dt>dataset</dt>
                <dd><code>{match.dataset}</code></dd>
              {/if}
              {#if match.plan_type}
                <dt>plan type</dt>
                <dd><code>{match.plan_type}</code></dd>
              {/if}
              <dt>registry</dt>
              <dd>
                <code>{match.registry}</code>
                <span class="quiet"> at submit</span>
              </dd>
              {#if data.escalated_at}
                <dt>new plan asked for</dt>
                <dd>{filed(data.escalated_at)}</dd>
              {/if}
            </dl>

            {#if match.would}
              <!-- The shortest path, written out: the caller's own values kept,
                 the gaps filled with placeholders from the schema. An example,
                 not a quote — the note under it is the server's, and it says so
                 every time rather than trusting the last person who looked. -->
              <div class="would-fit">
                <p class="micro">
                  the closest fit — <code>{match.would.plan_type}</code>
                </p>
                <dl class="kv">
                  {#each match.would.params as param (param.key)}
                    <dt>{param.key}</dt>
                    <dd><code>{param.value}</code></dd>
                  {/each}
                </dl>
                {#if match.would.note}
                  <p class="quiet note">{match.would.note}</p>
                {/if}
              </div>
            {/if}

            {#if match.candidates.length}
              <ul class="candidates">
                {#each match.candidates as candidate (candidate.plan_type)}
                  <li class="candidate">
                    <span class="pill {TONES[candidate.verdict] ?? ''}"
                      >{candidate.verdict}</span
                    >
                    <code class="type">{candidate.plan_type}</code>
                    <span class="ver">v{candidate.version}</span>
                    {#if candidate.missing.length}
                      <span class="detail"
                        >missing {candidate.missing.join(", ")}</span
                      >
                    {:else if candidate.reason}
                      <span class="detail">{candidate.reason}</span>
                    {/if}
                  </li>
                  {#each candidate.rejected as rejection (rejection.where)}
                    <!-- The validator's own words: a caller cannot see their
                       value is outside the enum from their own request. -->
                    <li class="candidate rejected">
                      <code class="type">{rejection.where}</code>
                      <span class="detail">{rejection.why}</span>
                    </li>
                  {/each}
                {/each}
              </ul>
            {/if}

            {#if !data.escalated_at && match.verdict !== "needs_params"}
              <!-- Not a button, and deliberately: asking for new code is a
                 request to a different owner, not a click on a read page. The
                 fact that matters is who asks, and through what door. -->
              <p class="quiet note">
                A new plan is asked for with
                <code>POST /v1/requests/{data.id}/escalate</code>, by a token
                holding the <code>escalate</code> scope.
              </p>
            {/if}
          {/if}
        </section>
      {/if}

      <section class="block">
        <p class="micro">events</p>
        <ol class="events">
          {#each data.events as event (event.id)}
            <li>
              <span class="when">{event.ts}</span>
              <span class="what">{event.event}</span>
              {#if who(event)}
                <span class="who">{who(event)}</span>
              {/if}
            </li>
          {/each}
        </ol>
      </section>

      {#if rawBody}
        <details class="raw">
          <summary class="micro">
            {data.payload.intent
              ? "the intent body, verbatim"
              : "the plan, as recorded"}
          </summary>
          <pre>{rawBody}</pre>
        </details>
      {/if}

      {#if data.identity?.job_hash}
        <p class="hash-row">
          <span class="micro">job hash</span>
          <code>{data.identity.job_hash}</code>
        </p>
      {/if}
    </DetailPanel>
  {/if}
</AppShell>

<style>
  .block {
    margin: 0 0 20px;
  }

  /* The request leads, so it gets a little more air than the sections that
     comment on it. */
  .request .kv {
    margin-top: 10px;
  }

  .spec dt {
    font-family: var(--font-mono, ui-monospace, monospace);
    font-size: 11.5px;
  }

  /* No figure to show: one sentence, where the rows would have been. */
  .unpriced {
    margin: 8px 0 0;
  }

  .why {
    margin: 6px 0 0;
    font-size: 14px;
    line-height: 1.55;
    color: var(--ink);
  }

  /* A failure is the loudest thing on the page and still not a siren: sunken
     rather than red, with the code in mono because it is a phrase the code
     owns and the reader may grep for it. The sentence keeps normal type, since
     it is prose a driver wrote. */
  .failed {
    padding: 10px 12px;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-left: 3px solid var(--bad);
    border-radius: var(--radius);
  }

  .failed-code {
    margin: 4px 0 0;
  }

  .failed-code code {
    font-size: 13px;
    word-break: break-word;
  }

  .failed-detail {
    margin: 6px 0 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-dim);
  }
  .kv {
    display: grid;
    grid-template-columns: minmax(7rem, max-content) 1fr;
    gap: 6px 14px;
    margin: 8px 0 0;
  }

  .kv dt {
    color: var(--ink-muted);
    font-size: 12px;
  }

  .kv dd {
    margin: 0;
    font-size: 13px;
  }

  .kv dd code {
    font-size: 12px;
    word-break: break-word;
  }

  /* The closest fit: indented and set back, because it is a branch the caller
     did not take and must not be read as the request itself. */
  .would-fit {
    margin: 14px 0 0;
    padding: 10px 12px;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-radius: var(--radius);
  }

  .would-fit .kv {
    margin-top: 6px;
  }

  .note {
    margin: 8px 0 0;
    font-size: 12px;
    line-height: 1.5;
  }

  .candidates {
    margin: 14px 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .candidate {
    display: flex;
    gap: 8px;
    align-items: baseline;
    flex-wrap: wrap;
    font-size: 12px;
    color: var(--ink-dim);
  }

  .candidate .type {
    font-size: 12px;
    color: var(--ink);
  }

  .candidate .ver {
    font-size: 11px;
    color: var(--ink-faint);
  }

  .candidate .detail {
    color: var(--ink-muted);
  }

  /* A refused value belongs under its type, not beside it. */
  .candidate.rejected {
    padding-left: 22px;
  }

  .events {
    margin: 8px 0 0;
    padding: 0;
    list-style: none;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .events li {
    display: flex;
    gap: 10px;
    align-items: baseline;
    font-size: 12px;
    color: var(--ink-dim);
  }

  .when {
    color: var(--ink-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .what {
    color: var(--ink);
  }

  .who {
    color: var(--ink-muted);
    font-size: 11px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill, 999px);
    padding: 0 6px;
  }

  .raw {
    margin: 0 0 16px;
  }
  .raw summary {
    cursor: pointer;
    color: var(--ink-muted);
  }

  .raw pre {
    margin: 8px 0 0;
    padding: 10px 12px;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    font-size: 11.5px;
    line-height: 1.5;
    overflow-x: auto;
    color: var(--ink-dim);
  }

  .hash-row {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .hash-row code {
    font-size: 11px;
    color: var(--ink-faint);
    word-break: break-all;
  }
</style>
