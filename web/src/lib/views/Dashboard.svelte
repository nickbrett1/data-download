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
  import { filed, stateDetail, stateLabel, stateTone, usd } from "$lib/format";

  const FACET_LABELS = {
    broken: "Broken",
    // Kept as a fallback only: the server sends `facet_labels` with every
    // view-model, and that is the one true list. A facet added in Python whose
    // key is never added here would otherwise reach the row as `needs_params`
    // with the underscore still in it.
    needs_params: "Needs parameters",
    no_plan: "No plan yet",
    requested: "Plan requested",
    todo: "Approval",
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
    // Two buckets that are not in `facet_order` -- one because a state this
    // code has never heard of renders last, the other because a cancelled row
    // needs nothing and is deliberately not in the book's default list. Both
    // are offered by name whenever there is one, so neither is unreachable.
    if ((data.facets.cancelled ?? []).length) order.push("cancelled");
    if ((data.facets.archived ?? []).length) order.push("archived");
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
    // `rows` is the book's default list, which the server already narrows (a
    // cancelled request is settled and needs nothing, so it is not in it). A
    // selected facet therefore reads from `facets`, or the one bucket the
    // server filters out could never be shown at all.
    if (show === "all") return data.rows;
    return data.facets[show] ?? [];
  });

  // Four columns: what it is, where it is, what it costs, when it was asked
  // for. The quote column spent the first three phases as a dash from top to
  // bottom, because nothing priced anything; phase 4 fills it at submit, so it
  // is a fact now rather than an empty cell dressed as one. It stays a dash
  // only where there is no price to show: a blocked row has no Job to quote and
  // the schema forbids it an estimate.
  //
  // A fifth -- "why it matters" -- was tried and removed: on a finished row it
  // repeated the badge or said nothing, and its useful half (what a blocked row
  // is short of) is a qualification of the state, so it lives under the badge
  // now. See `stateDetail`.
  const columns = [
    { label: "request", width: "minmax(11rem, 1.9fr)" },
    { label: "state", width: "minmax(9rem, 0.9fr)" },
    { label: "quote", align: "end", width: "minmax(5rem, 0.5fr)" },
    { label: "filed", align: "end" },
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
    { label: "Datasets", href: "/datasets", current: false },
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
          {@const detail = stateDetail(request)}
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
          <!-- The badge, and under it the one thing the badge cannot say: what a
               blocked row is short of, whether an approved row was ever claimed,
               or the ceiling a draft is asking to authorise. Most rows have
               nothing to add, and say nothing rather than showing a dash. -->
          <span class="state">
            <span class="pill {stateTone(request.state)}"
              >{stateLabel(request.state)}</span
            >
            {#if detail}
              <span class="detail {detail.tone}" title={detail.title ?? ""}
                >{detail.text}</span
              >
            {/if}
          </span>
          <!-- The quote is a number, and it is the number the ceiling was
               compared against. A row with nothing to quote says so with a
               dash, not a zero: `$0.00` would read as a free download. -->
          <span class="num quote">{usd(request.estimate_usd)}</span>
          <span class="num filed">{filed(request.created_at)}</span>
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

  /* The badge and its detail share a cell, so they stack rather than fight for
     the same line. `min-width: 0` is what lets the detail clip instead of
     pushing the `filed` column off the grid. */
  .state {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 3px;
    min-width: 0;
  }

  /* Small, quiet, and clipped: this is a qualifier, not a second headline. The
     colour is the whole signal a second pill used to carry -- a stuck `running`
     row must not look like an ordinary one. */
  .detail {
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 11px;
    line-height: 1.3;
    color: var(--ink-muted);
  }

  .detail.info {
    color: var(--accent);
  }

  .detail.warn {
    color: var(--warn);
  }

  .detail.bad {
    color: var(--bad);
  }
</style>
