<script>
  /**
   * Plan types, each with the drivers that answer it.
   *
   * A **fixed list**, not a filter and not a picker. There are only a handful
   * of plan types by design -- they are a closed set in git -- so everything is
   * on one page, in order, with no selection to make before you can read
   * anything. A rail was tried and it was worse: a list of names beside a pane
   * reads like a set of filters, and the name looked like it belonged to the
   * status drawn next to it.
   *
   * The nesting is the model: a section is a *plan type* (the question), and
   * the rows inside it are *drivers* (one vendor's implementation of that
   * question). A driver is never a sibling of a plan type.
   *
   * The parameter table is the type's, and sits folded at the foot of its
   * section: it is reference material, and expanded it pushed the drivers off
   * the screen.
   *
   * Nothing sits to the right of a heading except the version. A status there
   * reads as an attribute of the name it shares a line with, so
   * "market_data_window  v1  driver" says the type is a driver. The rows below
   * already answer it: a driver row, or a row saying no driver is installed.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView } from "$lib/api";

  let data = $state(null);
  let failure = $state("");

  // `#<type>` is an anchor into the list (the dashboard links to it);
  // `#<type>/<driver>` also opens that driver's row.
  let hash = $state(
    typeof window === "undefined" ? "" : window.location.hash.replace(/^#/, ""),
  );

  $effect(() => {
    if (typeof window === "undefined") return;
    const read = () => (hash = window.location.hash.replace(/^#/, ""));
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  });

  $effect(() => {
    const controller = new AbortController();
    getView("/ui/plan-types.json", { signal: controller.signal })
      .then((body) => (data = body))
      .catch((error) => {
        if (error.name !== "AbortError") failure = error.message;
      });
    return () => controller.abort();
  });

  const fromHash = $derived(hash.includes("/") ? hash.split("/") : ["", ""]);
  const hashType = $derived(fromHash[0]);
  const hashDriver = $derived(fromHash[1] || "");

  // Which driver row is open, remembered per type so closing one stays closed.
  // `""` is an explicit "I closed it", which is a different fact from "no
  // choice yet" -- without that distinction the default row springs back open
  // the moment it is shut.
  let chosen = $state({});

  // Artefacts the runtime has but no plan type accepts. Not an error and not
  // hidden: "why is this driver doing nothing" is a question this page should
  // be able to answer.
  const orphans = $derived(
    (data?.drivers?.installed ?? []).filter((row) => row.answers.length === 0),
  );

  const columns = [
    { label: "parameter", width: "minmax(8rem, 0.9fr)" },
    { label: "type", width: "7.5rem" },
    { label: "constraints", width: "minmax(9rem, 1.1fr)" },
    { label: "what it means", width: "minmax(12rem, 2fr)" },
  ];

  function openDriverFor(type) {
    if (Object.hasOwn(chosen, type.name)) return chosen[type.name];
    if (hashType === type.name) return hashDriver;
    return type.implementations.find((impl) => impl.driver)?.driver?.name || "";
  }

  function toggle(type, impl, open) {
    chosen = { ...chosen, [type.name]: open ? "" : impl.driver.name };
    if (typeof window === "undefined") return;
    const fragment = open ? type.name : `${type.name}/${impl.driver.name}`;
    window.location.hash = fragment;
    hash = fragment;
  }
</script>

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

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the registry…</p>
  {:else if data.types.length === 0}
    <EmptyState
      customMessage="No plan types are registered — every submission will be rejected."
    />
  {:else}
    {#each data.types as type (type.name)}
      <section class="plan-type" id={type.anchor}>
        <div class="type-head">
          <h2>{type.name}</h2>
          {#if type.vendor}
            <span class="pill">{type.vendor}</span>
          {/if}
          <span class="pill">v{type.version}</span>
        </div>
        <p class="type-desc">{type.description}</p>

        <h3 class="sub-head">drivers</h3>
        {#if type.implementations.length === 0}
          <p class="hint">
            No vendor is listed for this type, so no driver can answer it.
          </p>
        {:else}
          {#if !data.drivers.known}
            <p class="hint">
              Driver state is unknown — {data.drivers.detail ||
                "the driver runtime did not answer"} — so the rows below say what
              each vendor <em>should</em> have installed, not what does.
            </p>
          {/if}
          {@const openName = openDriverFor(type)}
          <ul class="drivers">
            {#each type.implementations as impl (impl.vendor)}
              {@const open = impl.driver && impl.driver.name === openName}
              <li class="driver-row" class:open>
                <button
                  type="button"
                  class="driver-line driver-toggle"
                  disabled={!impl.driver}
                  aria-expanded={open}
                  onclick={() => impl.driver && toggle(type, impl, open)}
                >
                  <span class="vendor">{impl.vendor}</span>
                  {#if impl.driver}
                    <span class="pill impl-on"
                      >{impl.driver.name} v{impl.driver.version}</span
                    >
                  {:else}
                    <span class="pill impl-off">no driver installed</span>
                  {/if}
                  <span class="spacer"></span>
                  <span class="micro"
                    >{impl.datasets.length} dataset
                    {impl.datasets.length === 1 ? "code" : "codes"}</span
                  >
                  {#if impl.driver}
                    <span class="chev" aria-hidden="true"
                      >{open ? "−" : "+"}</span
                    >
                  {/if}
                </button>
                {#if open}
                  <!-- The match as a rule rather than as prose: this vendor,
                       and one of these dataset codes. Nothing else: the name
                       and version are already the row's pill, protocol can
                       only be 1 here (a mismatch refuses discovery outright),
                       and the install date is a clock, not an ordering. -->
                  <dl class="driver-detail">
                    <dt>matches</dt>
                    <dd>
                      vendor <code>{impl.vendor}</code>
                      {#if impl.datasets.length}
                        · dataset is one of
                        {#each impl.datasets as code (code)}
                          <code class="code-chip">{code}</code>
                        {/each}
                      {:else}
                        · any dataset
                      {/if}
                    </dd>
                  </dl>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}

        <details class="asks">
          <summary>
            what the type asks · {type.schema.required_count} required, {type
              .schema.optional_count} optional
          </summary>
          {#if type.schema.fields.length}
            <DataTable {columns} items={type.schema.fields}>
              {#snippet row(field)}
                <span class="name"
                  ><code>{field.name}</code>{#if field.required}<span
                      class="req">required</span
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
          <details class="raw">
            <summary>raw JSON schema</summary>
            <pre>{type.schema.raw}</pre>
          </details>
        </details>
      </section>
    {/each}

    {#if orphans.length}
      <!-- A section rather than a place in the list above, because these are
           artefacts, not questions: nothing here can be filed as them. -->
      <section class="plan-type orphans" id="unmatched">
        <div class="type-head">
          <h2>drivers with no plan type</h2>
        </div>
        <p class="type-desc">
          Installed in the runtime, but no plan type above lists their vendor,
          so nothing can be filed as them.
        </p>
        <ul class="drivers">
          {#each orphans as row (row.name + row.version)}
            <li class="driver-row">
              <div class="driver-line">
                <code class="driver-name">{row.name}</code>
                <span class="pill">v{row.version}</span>
                <span class="vendor">{row.vendor || "—"}</span>
              </div>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</AppShell>

<style>
  /* One list, in order. A type is a section, not a card: cards read as peers
     that happen to be side by side, and the point is that this is the whole
     fixed set, top to bottom. */
  .plan-type {
    padding: 0 0 18px;
    margin: 0 0 18px;
    border-bottom: 1px solid var(--line);
    /* The dashboard links straight to /plan-types#<type>, and the bar is
       sticky, so without this the heading lands underneath it. */
    scroll-margin-top: 72px;
  }

  .plan-type:last-child,
  .orphans {
    border-bottom: 0;
    padding-bottom: 0;
    margin-bottom: 0;
  }

  .type-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 4px;
  }

  .type-head h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--ink);
  }

  .type-head .spacer,
  .driver-line .spacer {
    flex: 1 1 auto;
  }

  .type-desc {
    margin: 0 0 10px;
    color: var(--ink-dim);
    max-width: 78ch;
  }

  /* "drivers" labels the list inside a section; it is not part of the page's
     outline, which is the plan types. */
  .sub-head {
    margin: 12px 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .drivers {
    list-style: none;
    margin: 0 0 12px;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  /* One row per driver. The row is the button that opens it, so the hit target
     is the whole line rather than a word in it. */
  .driver-row {
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    background: var(--bg-sunken);
    overflow: hidden;
  }

  .driver-row.open {
    border-color: var(--accent);
  }

  .driver-line {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    width: 100%;
    padding: 8px 10px;
    font: inherit;
    font-size: 12px;
    color: var(--ink);
    text-align: left;
    background: transparent;
    border: 0;
  }

  .driver-toggle {
    cursor: pointer;
  }

  .driver-toggle:disabled {
    cursor: default;
    color: var(--ink-dim);
  }

  .driver-name,
  .vendor {
    font-weight: 600;
    color: var(--ink);
  }

  .chev {
    color: var(--ink-muted);
    font-variant-numeric: tabular-nums;
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

  /* The open driver: a small definition list, because each line answers one
     question and the answers do not need prose around them. */
  .driver-detail {
    display: grid;
    grid-template-columns: 4.5rem minmax(0, 1fr);
    gap: 4px 10px;
    margin: 0;
    padding: 0 10px 10px;
    font-size: 12px;
    color: var(--ink-dim);
  }

  .driver-detail dt {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-muted);
    padding-top: 2px;
  }

  .driver-detail dd {
    margin: 0;
    overflow-wrap: anywhere;
  }

  .code-chip {
    display: inline-block;
    margin: 0 2px;
    padding: 0 5px;
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 11px;
  }

  /* The field table is reference, not the answer: folded, so what a type asks
     never pushes the next type off the screen. */
  .asks {
    margin-top: 10px;
  }

  .asks > summary,
  .raw > summary {
    cursor: pointer;
    color: var(--ink-muted);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }

  .asks > summary:hover,
  .raw > summary:hover {
    color: var(--ink);
  }

  .asks[open] > summary {
    margin-bottom: 8px;
  }

  .raw {
    margin-top: 10px;
  }

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

  pre {
    margin: 8px 0 0;
    padding: 10px;
    overflow: auto;
    background: var(--bg-sunken);
    border: 1px solid var(--line);
    border-radius: var(--radius-sm);
    font-size: 11px;
  }

  @media (max-width: 720px) {
    .driver-detail {
      grid-template-columns: minmax(0, 1fr);
      gap: 2px;
    }

    .driver-detail dt {
      padding-top: 6px;
    }
  }
</style>
