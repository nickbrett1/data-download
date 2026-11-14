<script>
  /**
   * The request book: every download request in one list, newest first.
   *
   * Why one list rather than four sections -- the four SQL views answer four
   * different questions, but a reader arrives with one ("what is going on?").
   * So the views stay the semantic truth on the server and this page flattens
   * them, where the interesting condition is a badge instead of a partition.
   *
   * Nothing here can approve or change a request: approval is a CLI verb with a
   * scope this page cannot reach, and the token in the URL is `read` only. That
   * fact shapes the page -- there is no button, and no line of prose saying so
   * either: a reader who came to look at the book does not need to be told what
   * the book cannot do.
   */
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import DataTable from "$lib/components/nb/DataTable.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import FilterPanel from "$lib/components/nb/FilterPanel.svelte";
  import { getView, withToken } from "$lib/api";
  import { age, stateTone, usd, whyItMatters } from "$lib/format";

  const FACET_LABELS = {
    broken: "Broken",
    waiting: "Waiting on a plan",
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
            label: FACET_LABELS[key] ?? key,
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

  const columns = [
    { label: "request", width: "minmax(9rem, 1.3fr)" },
    { label: "state", width: "7.5rem" },
    { label: "type", width: "minmax(7rem, 1fr)" },
    { label: "estimate", align: "end" },
    { label: "age", align: "end" },
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
    { label: "Requests", href: withToken("/"), current: true },
    { label: "Plan types", href: withToken("/plan-types"), current: false },
  ]}
>
  <p class="lede">The request book</p>

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Loading the book…</p>
  {:else}
    <FilterPanel
      {facets}
      {selected}
      onchange={(next) => setShow((next.show ?? [])[0] ?? "all")}
      onclear={() => setShow("all")}
    />

    {#if rows.length}
      <DataTable
        title="Requests"
        {columns}
        items={rows}
        emptyMessage="No requests yet."
      >
        {#snippet row(request)}
          {@const why = whyItMatters(request)}
          <span class="id"><code title={request.id}>{request.id}</code></span>
          <span
            ><span class="pill {stateTone(request.state)}">{request.state}</span
            ></span
          >
          <span>
            {#if request.plan_type}
              <a href={withToken(`/plan-types#${request.plan_type}`)}
                >{request.plan_type}</a
              >
            {:else}
              —
            {/if}
          </span>
          <span class="num">{usd(request.estimate_usd)}</span>
          <span class="num">{age(request.age_seconds)}</span>
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
    {:else}
      <!-- Let the block say it: "No requests yet" is a different sentence from
           "no matches for active filters", and it already knows which is which. -->
      <EmptyState
        cause={show === "all" ? "empty" : "filters"}
        customMessage={show === "all" ? "No requests yet." : ""}
        onclear={() => setShow("all")}
      />
    {/if}
  {/if}
</AppShell>
