<script>
  /**
   * The request book: every download request in one list, newest first.
   *
   * Why one list rather than four sections -- the four SQL views answer four
   * different questions, but a reader arrives with one ("what is going on?").
   * So the views stay the semantic truth on the server and this page flattens
   * them, where the interesting condition is a badge instead of a partition.
   *
   * Nothing here can approve or change a request: this page is a public
   * read-only projection of the store, and the one verb the app has lives on a
   * single request's page, behind an `approve` token no agent holds.
   * That fact shapes the page -- there is no button, and no line of prose
   * saying so either: a reader who came to look at the book does not need to be
   * told what the book cannot do.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import FilterPanel from "$lib/components/nb/FilterPanel.svelte";
  import { getView } from "$lib/api";
  import { filed, stateTone, whyItMatters } from "$lib/format";

  const FACET_LABELS = {
    broken: "Broken",
    // Kept as a fallback only: the server sends `facet_labels` with every
    // view-model, and that is the one true list. A facet added in Python whose
    // key is never added here would otherwise reach the row as `needs_params`
    // with the underscore still in it.
    needs_params: "Needs parameters",
    no_plan: "No plan yet",
    requested: "Plan requested",
    todo: "To approve",
    working: "Running",
    done: "Finished",
    other: "Other",
  };

  let data = $state(null);
  let failure = $state("");
  let show = $state(currentShow());

  function currentShow() {
    if (typeof window === "undefined") return "all";
    return new URLSearchParams(window.location.search).get("show") || "all";
  }

  // `show` is the old page's query parameter, kept because it is a URL: it
  // survives a refresh, a bookmark, and a link pasted into a chat.
  function setShow(next) {
    show = next;
    const url = new URL(window.location.href);
    if (next === "all") url.searchParams.delete("show");
    else url.searchParams.set("show", next);
    window.history.replaceState({}, "", url);
  }

  $effect(() => {
    const controller = new AbortController();
    getView("/ui/dashboard.json", { signal: controller.signal })
      .then((body) => {
        data = body;
        // A filter from a stale link may no longer select anything.
        const known = ["all", ...body.facet_order, "other"];
        if (!known.includes(show)) setShow("all");
      })
      .catch((error) => {
        if (error.name !== "AbortError") failure = error.message;
      });
    return () => controller.abort();
  });

  const facets = $derived.by(() => {
    if (!data) return [];
    const order = [...data.facet_order];
    if ((data.facets.other ?? []).length) order.push("other");
    return [
      {
        key: "show",
        label: "Show",
        mode: "single",
        layout: "bars",
        note: `${data.total} request${data.total === 1 ? "" : "s"}, by what each one needs`,
        options: [
          { value: "all", label: "All", count: data.total },
          ...order.map((key) => ({
            value: key,
            label: data.facet_labels?.[key] ?? FACET_LABELS[key] ?? key,
            count: (data.facets[key] ?? []).length,
          })),
        ],
      },
    ];
  });

  const selected = $derived({ show: [show] });

  const rows = $derived.by(() => {
    if (!data) return [];
    if (show === "all") return data.rows;
    return data.rows.filter((row) => row.facet === show);
  });

  // No estimate column yet. `estimate_usd` is NULL on every row until a phase
  // prices something, so the column was a dash from top to bottom -- an empty
  // cell pretending to be a fact. The detail page still shows the estimate and
  // the ceiling, and the column comes back the day there is a number in it.
  const columns = [
    { label: "request", width: "minmax(11rem, 1.6fr)" },
    { label: "state", width: "7.5rem" },
    { label: "filed", align: "end" },
    { label: "why it matters", width: "minmax(10rem, 1.7fr)" },
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
    { label: "Requests", href: "/", current: true },
    { label: "Plan types", href: "/plan-types", current: false },
  ]}
>
  {#snippet mark()}
    <AppMark />
  {/snippet}

  <p class="lede">The request book</p>

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the book…</p>
  {:else}
    {#if data.total}
      <!-- A filter strip is a way to narrow a list. Over an empty book it is
           six zeroes that cannot be clicked into anything useful, so it waits
           until there is something to narrow. -->
      <FilterPanel
        {facets}
        {selected}
        onchange={(next) => setShow((next.show ?? [])[0] ?? "all")}
        onclear={() => setShow("all")}
      />
    {/if}

    {#if rows.length}
      <DataTable
        title="Requests"
        {columns}
        items={rows}
        emptyMessage="No requests yet."
      >
        {#snippet row(request)}
          {@const why = whyItMatters(request)}
          <span>
            <!-- A name, not a key. The id is opaque by design -- it is the
                 durable handle, not a label -- so the row leads with `#seq` and
                 the subject, the second line says who is being asked, and the
                 id survives only as the tooltip. The link's `::after` covers
                 the row, so the whole line is the target rather than the three
                 words in it. -->
            <a class="req" href={`/requests/${request.id}`} title={request.id}>
              <span class="req-num">#{request.seq}</span>
              <span class="req-subject">{request.label}</span>
            </a>
            <span class="req-vendor">{request.vendor || "—"}</span>
          </span>
          <span
            ><span class="pill {stateTone(request.state)}">{request.state}</span
            ></span
          >
          <span class="num filed">{filed(request.created_at)}</span>
          <span>
            {#if why}
              <span class="pill {why.tone}" title={why.title ?? ""}
                >{why.text}</span
              >
            {:else}
              <span class="quiet">—</span>
            {/if}
          </span>
        {/snippet}
      </DataTable>
    {:else if show === "all"}
      <!-- An empty book used to say it three times: the block's "zero results"
           label, ours "No requests yet.", and the block's stock hint. There is
           one sentence worth saying here, and it is the one the stock hint
           cannot: which door a request actually comes through. There is no CLI
           in this service - `POST /v1/requests` is the door, and a reader
           staring at an empty book is the one person who needs to know it. -->
      <p class="empty-book">
        No requests yet — requests arrive through the API (<code
          >POST /v1/requests</code
        >), not from this page.
      </p>
    {:else}
      <!-- Under a filter the block earns its place: it offers the way out. -->
      <EmptyState cause="filters" onclear={() => setShow("all")} />
    {/if}
  {/if}
</AppShell>

<style>
  /* The one line an empty book gets. Not a card: there is no state to
     distinguish, no action to offer, and the filter strip above it is already
     absent. */
  .empty-book {
    margin: 18px 0 0;
    color: var(--ink-muted);
    font-size: 13px;
  }

  /* The name is the link, and the link is the row: `::after` stretches it over
     the nearest positioned ancestor, which is why `:global(.tbl-row)` is
     relative. One anchor, one tab stop, and a hit area the width of the line.

     The first cell has to stop clipping for that to work -- the block's
     `.tbl-row :global(*)` rule gives every cell `overflow: hidden` so a long
     value cannot shift the columns. `.req-subject` clips itself instead, which
     keeps the ellipsis and lets the overlay escape. */
  :global(.tbl-row:has(.req)) {
    position: relative;
    cursor: pointer;
  }

  :global(.tbl-row:has(.req) > :first-child) {
    overflow: visible;
  }

  .req {
    display: inline-flex;
    align-items: baseline;
    gap: 7px;
    max-width: 100%;
    text-decoration: none;
    color: var(--ink);
  }

  .req::after {
    content: "";
    position: absolute;
    inset: 0;
  }

  /* Hover belongs to the row, because the row is what the link covers. */
  :global(.tbl-row:has(.req):hover) .req-subject {
    color: var(--accent);
    text-decoration: underline;
  }

  .req-num {
    font-variant-numeric: tabular-nums;
    color: var(--ink-faint);
    font-size: 11.5px;
  }

  .req-subject {
    font-size: 13px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* The vendor under the name: the same place the opaque key used to sit, and
     the answer to the question the key never answered. */
  .req-vendor {
    display: block;
    margin-top: 2px;
    font-size: 10.5px;
    color: var(--ink-muted);
  }

  .filed {
    color: var(--ink-dim);
    white-space: nowrap;
  }
</style>
