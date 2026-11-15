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
  import AppMark from "$lib/components/AppMark.svelte";
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

<!--
  `footer={false}`: this page ends where its content ends. The house strip is
  app-shell's default, and this app's own tests assert there is none -- which
  used to be true only because the block had been edited in place after it was
  installed. It is a supported state since 1.12.0.
-->
<AppShell
  footer={false}
  title="data-download"
  nav={[
    { label: "Requests", href: withToken("/"), current: false },
    { label: "Plan types", href: withToken("/plan-types"), current: true },
  ]}
>
  {#snippet mark()}
    <AppMark />
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
    {#if data.types.length > 1}
      <!-- A table of contents for one entry is not a contents: clicking it
           scrolled the page by a few pixels and lit the chip under the cursor,
           which reads as a broken link rather than as navigation. Kept for the
           day the registry holds more than one shape. -->
      <nav class="toc" aria-label="Plan types">
        {#each data.types as type (type.name)}
          <a href="#{type.anchor}">{type.name}</a>
        {/each}
      </nav>
    {/if}

    {#each data.types as type (type.name)}
      <section class="card type-card" id={type.anchor}>
        <div class="type-head">
          <h2>{type.name}</h2>
          {#if type.vendor}<span class="pill">{type.vendor}</span>{/if}
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
    /* .card is a surface, not a box with room in it: without this the h2 sits
       on the border. */
    padding: 14px 16px 12px;
    margin: 0 0 14px;
    /* A dashboard row links straight to /plan-types#<type>. The bar is sticky,
       so without this the card's own heading lands underneath it and the jump
       looks like it went one row too far. */
    scroll-margin-top: 72px;
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
</style>
