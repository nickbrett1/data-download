<script>
  /**
   * One request, in full.
   *
   * The list is a list of keys -- `#7`, what was asked for, and a badge for why
   * it is interesting. This is the answer to clicking one: the human `why`, the
   * body the caller actually sent, the identity columns, and the append-only
   * trail. Read scope, GET only, and nothing on this page can change a request:
   * approval is still a CLI verb with no endpoint here.
   *
   * The id is shown in full and in mono, because this is the page where you copy
   * it into a curl. Everywhere else it is a tooltip, because a key is not a name.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DetailPanel from "$lib/components/nb/DetailPanel.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView, withToken } from "$lib/api";
  import { age, stateTone, usd } from "$lib/format";

  let { id } = $props();

  let data = $state(null);
  let failure = $state("");

  $effect(() => {
    const controller = new AbortController();
    getView(`/ui/request/${encodeURIComponent(id)}.json`, {
      signal: controller.signal,
    })
      .then((body) => {
        data = body;
      })
      .catch((error) => {
        if (error.name !== "AbortError") failure = error.message;
      });
    return () => controller.abort();
  });

  const meta = $derived.by(() => {
    if (!data) return [];
    const rows = [
      { label: "request id", value: data.id, isCode: true },
      { label: "state", value: data.state },
      {
        label: data.facet === "waiting" ? "waiting on" : "why it matters",
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
      data.payload?.intent ?? data.payload?.plan ?? data.payload?.params;
    return body ? JSON.stringify(body, null, 2) : "";
  });
</script>

<AppShell
  footer={false}
  title="data-download"
  nav={[
    { label: "Requests", href: withToken("/"), current: true },
    { label: "Plan types", href: withToken("/plan-types"), current: false },
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
      pill={{ label: data.state, tone: stateTone(data.state) }}
    >
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

      {#if data.identity?.plan_hash}
        <p class="hash-row">
          <span class="micro">plan hash</span>
          <code>{data.identity.plan_hash}</code>
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
