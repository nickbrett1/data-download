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
  import AppMark from "$lib/components/AppMark.svelte";
  import ApproveCard from "$lib/components/ApproveCard.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DetailPanel from "$lib/components/nb/DetailPanel.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView } from "$lib/api";
  import { age, filed, stateLabel, stateTone, usd } from "$lib/format";

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

  const meta = $derived.by(() => {
    if (!data) return [];
    const rows = [
      { label: "request id", value: data.id, isCode: true },
      { label: "state", value: stateLabel(data.state) },
      {
        label: BLOCKED.has(data.facet) ? "waiting on" : "why it matters",
        value: data.facet_label,
      },
    ];
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
    rows.push(
      { label: "ceiling", value: usd(data.max_usd) },
      { label: "estimate", value: usd(data.estimate_usd) },
      { label: "filed", value: data.created_at },
      { label: "age", value: age(data.age_seconds) },
    );
    return rows;
  });

  const asked = $derived.by(() => {
    if (!data) return [];
    const params = data.payload?.params ?? {};
    return Object.entries(params).map(([key, value]) => ({
      key,
      value: typeof value === "string" ? value : JSON.stringify(value),
    }));
  });

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
      {#if data.state === "draft"}
        <!-- The one verb on this page. Rendered only where it can be used: a
             row that is already approved gets no control, because a second
             approval is a no-op and a disabled button would be a promise the
             page cannot keep. -->
        <ApproveCard
          requestId={data.id}
          maxUsd={data.max_usd}
          onapproved={() => load()}
        />
      {/if}

      <section class="block">
        <p class="micro">why</p>
        {#if data.payload?.why}
          <p class="why">{data.payload.why}</p>
        {:else}
          <p class="quiet">
            No reason was recorded. Every submit takes a `why` — a plan filed
            before this was stored has none.
          </p>
        {/if}
      </section>

      {#if asked.length}
        <section class="block">
          <p class="micro">what was asked for</p>
          <dl class="kv">
            {#each asked as item (item.key)}
              <dt>{item.key}</dt>
              <dd><code>{item.value}</code></dd>
            {/each}
          </dl>
        </section>
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
              <span class="who">{event.actor_kind}</span>
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

  .why {
    margin: 6px 0 0;
    font-size: 14px;
    line-height: 1.55;
    color: var(--ink);
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
