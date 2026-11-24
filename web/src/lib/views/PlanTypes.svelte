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
  import { getView } from "$lib/api";

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
    { label: "Requests", href: "/", current: false },
    { label: "Datasets", href: "/datasets", current: false },
    { label: "Plan types", href: "/plan-types", current: true },
  ]}
>
  {#snippet mark()}
    <AppMark />
  {/snippet}

  <p class="lede">Plan types</p>
  <p class="sub-lede">
    Two things, deliberately separate. A <strong>plan type</strong> is the
    question — which instruments, which records, what window — and there is a
    small closed set of them, in git (<code>plan_types/*.yaml</code>). A
    <strong>driver</strong> is one seller's implementation of that question: a versioned
    artefact in the driver runtime that answers it with real vendor calls. A type
    nobody has installed a driver for can still be read here, and cannot yet be run.
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
          <!-- A pinned seller is the type's identity, so it is the pill. A
               `vendors:` map is who claims which dataset codes for matching,
               which is a different fact: the question itself has no vendor in
               it, and saying "Databento" here made the generic type look like
               a Databento type. The codes are itemised below the description,
               where a reader can see that they are a lookup table. -->
          {#if type.vendor}
            <span class="pill">{type.vendor}</span>
          {/if}
          <span class="pill">v{type.version}</span>
          <span class="spacer"></span>
          <!-- The seller count and the installed count are the two facts that
               used to be blended into one pill. Kept apart: the first is the
               question's, the second is the runtime's. -->
          <span class="micro">
            {type.schema.required_count} required · {type.schema.optional_count} optional
          </span>
          <span class="micro">
            {type.implementations.length}
            {type.implementations.length === 1 ? "seller" : "sellers"} ·
            {type.installed_count} with a driver
          </span>
        </div>
        <p class="type-desc">{type.description}</p>

        <!-- Who answers it, as rows rather than as a footnote on the pill:
             one seller, the artefact that serves it, and — when there is none —
             that said out loud, because "no driver installed" is why a request
             for a perfectly good plan type would wait. -->
        <h3 class="sub-head">answered by</h3>
        {#if type.implementations.length === 0}
          <p class="hint">
            No seller claims this type yet, so nothing can be filed as it.
          </p>
        {:else}
          <ul class="impls">
            {#each type.implementations as impl (impl.vendor)}
              <li class="impl">
                <span class="impl-vendor">{impl.vendor}</span>
                {#if impl.driver}
                  <span class="pill impl-on"
                    >driver {impl.driver.name} v{impl.driver.version}</span
                  >
                {:else}
                  <span class="pill impl-off">no driver installed</span>
                {/if}
                {#if impl.datasets.length}
                  <code class="impl-codes">{impl.datasets.join(" · ")}</code>
                {/if}
              </li>
            {/each}
          </ul>
          {#if !data.drivers.known}
            <p class="hint">
              Driver state is unknown — {data.drivers.detail ||
                "the driver runtime did not answer"} — so the rows above say what
              each seller <em>claims</em>, not what is installed.
            </p>
          {/if}
        {/if}

        <h3 class="sub-head">what it asks</h3>
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

  /* The question and its implementations get their own headings, because the
     whole point of this page is that they are not the same kind of thing. */
  .sub-head {
    margin: 14px 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .impls {
    list-style: none;
    margin: 0 0 14px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* One row per seller: the name in ink, the artefact as a pill (it is a
     versioned install, so it is a status, not prose), the dataset codes in
     mono because a code is a value you copy. */
  .impl {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    padding: 5px 10px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: var(--bg-sunken);
    font-size: 12px;
  }

  .impl-vendor {
    color: var(--ink);
    font-weight: 600;
  }

  .impl-on {
    color: var(--accent);
    background: var(--accent-soft);
  }

  /* Not installed is not an error: the type is still readable, and a request
     filed as it would wait for the driver rather than fail. */
  .impl-off {
    color: var(--ink-muted);
    background: transparent;
    border: 1px dashed var(--line);
  }

  .impl-codes {
    color: var(--ink-dim);
    font-size: 11px;
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
