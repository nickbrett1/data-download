<script>
  /**
   * Plan types: the closed set of shapes a request may be filed as.
   *
   * Read scope only, and the same data is on `GET /v1/plan-types` for machines.
   * This page exists because "which fields does a Databento plan need?" should
   * not require reading raw JSON in a terminal.
   *
   * The field table is DataTable -- four columns, one of them prose, and the
   * constraint cell is the reason a table rather than a list: two enums that
   * belong to the same parameter have to line up to be compared. The prose
   * around it (the definition list, the raw schema in a <details>) is not a
   * widget and stays here.
   */
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView, withToken } from "$lib/api";

  let data = $state(null);
  let failure = $state("");

  $effect(() => {
    const controller = new AbortController();
    getView("/ui/plan-types.json", { signal: controller.signal })
      .then((body) => (data = body))
      .catch((error) => {
        if (error.name !== "AbortError") failure = error.message;
      });
    return () => controller.abort();
  });

  const columns = [
    { label: "parameter", width: "minmax(8rem, 0.9fr)" },
    { label: "type", width: "7.5rem" },
    { label: "constraints", width: "minmax(9rem, 1.1fr)" },
    { label: "what it means", width: "minmax(12rem, 2fr)" },
  ];
</script>

<AppShell
  title="data-download"
  subtitle="plan types"
  nav={[
    { label: "Requests", href: withToken("/"), current: false },
    { label: "Plan types", href: withToken("/plan-types"), current: true },
  ]}
>
  {#snippet footer()}
    {#if data}
      <p>
        Machine-readable: <code>GET /v1/plan-types</code> (index) and
        <code>GET /v1/plan-types/&#123;type&#125;</code> (one schema).
      </p>
      <p class="muted">
        generated {data.generated_at} · read-only, no authority
      </p>
    {/if}
  {/snippet}

  <p class="lede">Plan types</p>
  <p class="sub-lede">
    The closed set of shapes a request may be filed as. Definitions live in git
    (<code>plan_types/*.yaml</code>) and are loaded at startup — a type that is
    not here cannot be filed, and its parameters are validated against the
    schema below before the request exists.
  </p>

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the registry…</p>
  {:else if data.types.length === 0}
    <EmptyState
      customMessage="No plan types are registered — every submission will be rejected."
    />
  {:else}
    <nav class="toc" aria-label="Plan types">
      {#each data.types as type (type.name)}
        <a href="#{type.anchor}">{type.name}</a>
      {/each}
    </nav>

    {#each data.types as type (type.name)}
      <section class="card type-card" id={type.anchor}>
        <div class="type-head">
          <h2>{type.name}</h2>
          <span class="pill">v{type.version}</span>
          <span class="spacer"></span>
          <span class="micro">
            {type.schema.required_count} required · {type.schema.optional_count} optional
          </span>
        </div>
        <p class="type-desc">{type.description}</p>

        {#if type.schema.fields.length}
          <DataTable {columns} items={type.schema.fields}>
            {#snippet row(field)}
              <span class="name"
                ><code>{field.name}</code>{#if field.required}<span class="req"
                    >required</span
                  >{/if}</span
              >
              <span class="type"><code>{field.type}</code></span>
              <span class="cons">
                {#if field.constraints.length}
                  {#each field.constraints as constraint, index (constraint)}
                    {constraint}{#if index < field.constraints.length - 1}<span
                        class="quiet"
                      >
                        ·
                      </span>{/if}
                  {/each}
                {:else}
                  <span class="quiet">—</span>
                {/if}
              </span>
              <span class="desc">{field.description || "—"}</span>
            {/snippet}
          </DataTable>
        {/if}

        <dl class="kv">
          <dt>extra fields</dt>
          <dd>
            {#if type.schema.closed}
              Rejected — only the parameters listed above may be sent, and
              anything else fails validation before the request is created.
            {:else}
              Accepted — parameters not listed above are passed through
              unvalidated.
            {/if}
          </dd>
          <dt>verify</dt>
          <dd>
            {type.verify.required ? "Required." : "Optional."}
            On failure:
            {#each type.on_failure as verb, index (verb)}<code>{verb}</code
              >{#if index < type.on_failure.length - 1},
              {/if}{/each}
            {#if type.on_failure.length === 0}nothing permitted{/if}.
            {#if type.verify.meaning}{type.verify.meaning}{/if}
          </dd>
          {#if type.resume}
            <dt>resume</dt>
            <dd>{type.resume}</dd>
          {/if}
          <dt>source</dt>
          <dd><code>{type.source}</code></dd>
        </dl>

        <details>
          <summary>Raw JSON schema</summary>
          <pre>{type.schema.raw}</pre>
        </details>
      </section>
    {/each}
  {/if}
</AppShell>

<style>
  .type-card {
    margin: 0 0 14px;
  }

  .type-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }

  .type-head h2 {
    margin: 0;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--ink);
  }

  .type-head .spacer {
    flex: 1 1 auto;
  }

  .type-desc {
    margin: 0 0 12px;
    color: var(--ink-dim);
    max-width: 78ch;
  }

  /* Required/optional is a property of a schema field, not a house primitive,
     so the tag lives here rather than in the global app.css — the pre-port
     Jinja page owned this rule too. An inline tag and not a column: DataTable drops the header
     row under 600px, and a column with no label says nothing on a phone. */
  .req {
    display: inline-block;
    margin-left: 6px;
    padding: 0 5px;
    border-radius: var(--radius-pill);
    font-size: 9px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--accent);
    background: var(--accent-soft);
  }

  /* A definition list, not a widget: four labels and the prose that explains
     them. It sits under the table because the table is the part a reader scans. */
  .kv {
    display: grid;
    grid-template-columns: 6.5rem minmax(0, 1fr);
    gap: 6px 12px;
    margin: 12px 0 0;
    padding-top: 10px;
    border-top: 1px solid var(--line);
  }

  .kv dt {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding-top: 1px;
  }

  .kv dd {
    margin: 0;
    color: var(--ink-dim);
    font-size: 12px;
    line-height: 1.45;
  }

  .kv dd code {
    color: var(--mark);
    font-size: 11px;
  }

  details {
    margin-top: 10px;
  }

  summary {
    cursor: pointer;
    color: var(--ink-muted);
    font-size: 11px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    font-weight: 600;
  }

  summary:hover {
    color: var(--accent);
  }

  details pre {
    margin: 8px 0 0;
    padding: 10px 12px;
    border-radius: var(--radius-sm);
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    color: var(--ink-muted);
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.5;
    overflow-x: auto;
  }

  .toc {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin: 0 0 16px;
  }

  .toc a {
    display: inline-flex;
    gap: 6px;
    align-items: baseline;
    padding: 3px 10px;
    border: 1px solid var(--line);
    border-radius: var(--radius-pill);
    background: var(--bg-sunken);
    color: var(--ink-dim);
    font-size: 12px;
    font-family: var(--mono);
  }

  .toc a:hover {
    background: var(--bg-hover);
    color: var(--accent);
    text-decoration: none;
  }

  @media (max-width: 820px) {
    .kv {
      grid-template-columns: 1fr;
      gap: 2px;
    }
  }
</style>
