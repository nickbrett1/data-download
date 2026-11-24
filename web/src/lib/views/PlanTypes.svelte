<script>
  /**
   * Plan types, and the drivers that answer them.
   *
   * Two levels, browsed rather than explained: a short list on the left, the
   * selected type on the right, and -- inside a type -- each matching driver
   * is a row you can open. The parameter table is the *type's*, so it sits
   * behind a disclosure instead of pushing the drivers off the screen; the
   * thing a reader comes here to answer is "which driver takes this, and is it
   * installed?".
   *
   * The selection lives in the fragment (`#<type>`, `#driver/<name>`, `#all`),
   * which is why the router in App.svelte has no hash handling: dashboard rows
   * already deep-link to `/plan-types#<type>`, and those links must keep
   * landing on the right card without a second routing system.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView } from "$lib/api";

  let data = $state(null);
  let failure = $state("");

  // "" is the fragment before a choice has been made; the first type wins it.
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

  // `#all` browses the runtime instead of a type: every artefact, including the
  // ones no plan type here asks for.
  const showAll = $derived(hash === "all");
  const driverMatch = $derived(hash.startsWith("driver/") ? hash.slice(7) : "");
  const typeName = $derived(
    driverMatch
      ? ""
      : (data?.types ?? []).some((type) => type.name === hash)
        ? hash
        : "",
  );
  const current = $derived(
    (data?.types ?? []).find((type) => type.name === typeName) ??
      (data?.types ?? [])[0] ??
      null,
  );
  // The driver rows of the selected type, newest artefact first.
  const implementations = $derived(current?.implementations ?? []);
  // Which driver row is open, remembered per type so that closing one stays
  // closed. `""` is an explicit "I closed it", which is a different fact from
  // "no choice yet" -- without that distinction the default row springs back
  // open the moment it is shut.
  let chosen = $state({});
  const defaultDriver = $derived(
    implementations.find((impl) => impl.driver)?.driver?.name || "",
  );
  const openDriver = $derived(
    Object.hasOwn(chosen, current?.name ?? "")
      ? chosen[current.name]
      : driverMatch || defaultDriver,
  );

  const columns = [
    { label: "parameter", width: "minmax(8rem, 0.9fr)" },
    { label: "type", width: "7.5rem" },
    { label: "constraints", width: "minmax(9rem, 1.1fr)" },
    { label: "what it means", width: "minmax(12rem, 2fr)" },
  ];

  function go(fragment) {
    if (typeof window === "undefined") return;
    window.location.hash = fragment;
    hash = fragment;
  }

  // How many drivers a type has, in words rather than as a fraction. "1/1" is
  // only legible if you already know what the two numbers are, and the answer
  // here is one thing, not a ratio.
  function driverCount(type) {
    const total = type.implementations.length;
    const have = type.installed_count;
    if (total === 0) return "";
    if (have === 0) return "no driver";
    if (have === total) return total === 1 ? "driver" : `${total} drivers`;
    return `${have} of ${total} drivers`;
  }

  // The same fact on the pane, spelled out, because there it is the answer to
  // the question rather than a hint in a rail.
  function driverSummary(type) {
    const total = type.implementations.length;
    const have = type.installed_count;
    // Nothing installed needs no summary: the row under it already says so,
    // and repeating it in the header reads as two problems instead of one.
    if (total === 0 || have === 0) return "";
    if (have === total) {
      return total === 1
        ? "driver installed"
        : `all ${total} drivers installed`;
    }
    return `${have} of ${total} drivers installed`;
  }

  function driverAnswers(row) {
    return (data?.types ?? []).filter((type) =>
      row.answers.includes(type.name),
    );
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
    <div class="browse">
      <!-- The list of questions, plus the runtime itself. A count per row is
           the whole "is this implemented?" answer at a glance. -->
      <nav class="rail" aria-label="Plan types">
        {#each data.types as type (type.name)}
          <button
            type="button"
            class="rail-item"
            class:on={!showAll && current?.name === type.name}
            aria-current={!showAll && current?.name === type.name
              ? "page"
              : undefined}
            onclick={() => go(type.name)}
          >
            <span class="rail-name">{type.name}</span>
            <span class="rail-count" class:off={type.installed_count === 0}
              >{driverCount(type)}</span
            >
          </button>
        {/each}
        <button
          type="button"
          class="rail-item rail-all"
          class:on={showAll}
          aria-current={showAll ? "page" : undefined}
          onclick={() => go("all")}
        >
          <span class="rail-name">all drivers</span>
          <span class="rail-count">{data.drivers.count}</span>
        </button>
      </nav>

      <div class="pane">
        {#if showAll}
          <h2 class="pane-head">installed drivers</h2>
          {#if !data.drivers.known}
            <p class="hint">
              Driver state is unknown — {data.drivers.detail ||
                "the driver runtime did not answer"}. Nothing below is evidence
              that nothing is installed.
            </p>
          {:else if data.drivers.installed.length === 0}
            <p class="hint">The runtime has no drivers installed.</p>
          {:else}
            <ul class="drivers">
              {#each data.drivers.installed as row (row.name + row.version)}
                {@const answered = driverAnswers(row)}
                <li class="driver-row">
                  <div class="driver-line">
                    <code class="driver-name">{row.name}</code>
                    <span class="pill">v{row.version}</span>
                    <span class="vendor">{row.vendor || "—"}</span>
                    <span class="spacer"></span>
                    <span class="micro">protocol {row.protocol}</span>
                    {#if row.installed_at}
                      <span class="micro"
                        >installed {row.installed_at.slice(0, 10)}</span
                      >
                    {/if}
                  </div>
                  <p class="answers">
                    {#if answered.length}
                      answers
                      {#each answered as type, index (type.name)}
                        <a href="/plan-types#{type.name}">{type.name}</a
                        >{#if index < answered.length - 1}<span class="quiet"
                            >,
                          </span>{/if}
                      {/each}
                    {:else}
                      <span class="quiet"
                        >no plan type here accepts {row.vendor ||
                          "its vendor"}</span
                      >
                    {/if}
                  </p>
                </li>
              {/each}
            </ul>
          {/if}
        {:else if current}
          <div class="pane-head">
            <h2>{current.name}</h2>
            {#if current.vendor}
              <span class="pill">{current.vendor}</span>
            {/if}
            <span class="pill">v{current.version}</span>
            <span class="spacer"></span>
            <span class="micro">{driverSummary(current)}</span>
          </div>
          <p class="type-desc">{current.description}</p>

          <h3 class="sub-head">drivers</h3>
          {#if implementations.length === 0}
            <p class="hint">
              No vendor is listed for this type, so no driver can answer it.
            </p>
          {:else}
            {#if !data.drivers.known}
              <p class="hint">
                Driver state is unknown — {data.drivers.detail ||
                  "the driver runtime did not answer"} — so the rows below say what
                what each vendor <em>should</em> have installed, not what does.
              </p>
            {/if}
            <ul class="drivers">
              {#each implementations as impl (impl.vendor)}
                {@const open = impl.driver && impl.driver.name === openDriver}
                <li class="driver-row" class:open>
                  <button
                    type="button"
                    class="driver-line driver-toggle"
                    disabled={!impl.driver}
                    aria-expanded={open}
                    onclick={() => {
                      if (!impl.driver) return;
                      chosen = {
                        ...chosen,
                        [current.name]: open ? "" : impl.driver.name,
                      };
                      go(
                        open
                          ? current.name
                          : `${current.name}/${impl.driver.name}`,
                      );
                    }}
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
                    <!-- What this driver answers, stated as a rule rather than
                         as prose: vendor + these dataset codes is the match. -->
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
                      <dt>artefact</dt>
                      <dd>
                        <code>{impl.driver.name}</code> v{impl.driver.version} · protocol
                        {impl.driver.protocol}
                        {#if impl.driver.installed_at}
                          · installed {impl.driver.installed_at.slice(0, 10)}
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
              what the type asks · {current.schema.required_count} required, {current
                .schema.optional_count} optional
            </summary>
            {#if current.schema.fields.length}
              <DataTable {columns} items={current.schema.fields}>
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
              <pre>{current.schema.raw}</pre>
            </details>
          </details>
        {/if}
      </div>
    </div>
  {/if}
</AppShell>

<style>
  /* Two columns: a short list, and the thing you picked. The rail is sticky
     because the pane scrolls past it and a list that scrolls away cannot be
     used to switch. */
  .browse {
    display: grid;
    grid-template-columns: minmax(11rem, 15rem) minmax(0, 1fr);
    gap: 16px;
    align-items: start;
  }

  .rail {
    position: sticky;
    top: 56px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .rail-item {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
    width: 100%;
    padding: 7px 10px;
    border: 1px solid transparent;
    border-radius: var(--radius-sm);
    background: transparent;
    color: var(--ink-dim);
    font: inherit;
    font-size: 12px;
    text-align: left;
    cursor: pointer;
  }

  .rail-item:hover {
    background: var(--bg-sunken);
    color: var(--ink);
  }

  /* The selected row is a surface, not a tint: with `aria-current` it is the
     only thing on the page saying where you are. */
  .rail-item.on {
    background: var(--bg-sunken);
    border-color: var(--line);
    color: var(--ink);
    font-weight: 600;
  }

  .rail-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* "driver" / "no driver" / "2 of 3 drivers". A word, because a fraction is
     only legible to someone who already knows what it counts. */
  .rail-count {
    flex: 0 0 auto;
    font-size: 10px;
    color: var(--accent);
  }

  /* Nothing installed is not an error, so it is quiet rather than red. */
  .rail-count.off {
    color: var(--ink-muted);
  }

  .rail-all {
    margin-top: 8px;
    border-top: 1px solid var(--line);
    border-radius: 0 0 var(--radius-sm) var(--radius-sm);
    padding-top: 10px;
  }

  .pane {
    min-width: 0;
  }

  .pane-head {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0 0 6px;
  }

  .pane-head h2 {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: -0.005em;
    color: var(--ink);
  }

  .pane-head .spacer,
  .driver-line .spacer {
    flex: 1 1 auto;
  }

  .type-desc {
    margin: 0 0 12px;
    color: var(--ink-dim);
    max-width: 78ch;
  }

  /* "drivers" is a label on a list, not a heading in the outline: the pane's
     own h2 is the type, and a second h3 per section made the page read as a
     document rather than as a browser. */
  .sub-head {
    margin: 14px 0 6px;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .drivers {
    list-style: none;
    margin: 0 0 14px;
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

  .driver-toggle:not(:disabled):hover {
    background: var(--bg-raised, transparent);
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

  .answers {
    margin: 0;
    padding: 0 10px 8px;
    font-size: 11px;
    color: var(--ink-muted);
  }

  .answers a {
    color: var(--accent);
  }

  /* The field table is reference, not the answer: folded, so the drivers stay
     above the fold on a phone. */
  .asks {
    margin-top: 18px;
    border-top: 1px solid var(--line);
    padding-top: 10px;
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

  /* One column on a phone: a rail beside a pane leaves neither enough width,
     and the rail becomes a horizontal strip above the pane instead. */
  @media (max-width: 720px) {
    .browse {
      grid-template-columns: minmax(0, 1fr);
    }

    .rail {
      position: static;
      flex-direction: row;
      overflow-x: auto;
      gap: 6px;
      padding-bottom: 4px;
    }

    .rail-item {
      width: auto;
      flex: 0 0 auto;
      border-color: var(--line);
    }

    .rail-all {
      margin-top: 0;
      border-top: 1px solid var(--line);
      border-radius: var(--radius-sm);
      padding-top: 7px;
    }

    .driver-detail {
      grid-template-columns: minmax(0, 1fr);
      gap: 2px;
    }

    .driver-detail dt {
      padding-top: 6px;
    }
  }
</style>
