<script>
  /**
   * Datasets: what landed, and what is still landing.
   *
   * The request book answers "what did I ask for?"; this page answers "what do
   * I have?", which is the question a phone actually asks. Both halves of the
   * answer come from the server (`/ui/datasets.json`): the store names the
   * vendor, dataset and state, and the datasets root holds the bytes and the
   * `landing.json` that says which driver wrote them.
   *
   * Read-only in every direction. The only verb here is a download, and every
   * file link goes through the server's `resolve_under` rather than being
   * assembled from a filesystem listing.
   *
   * Two presentation decisions worth stating:
   *
   * - A `running` request with three of five days on disk lists the three. An
   *   empty list under a running badge reads as "not started".
   * - Stub landings say so, once, on the card. Placeholder bytes next to real
   *   ones must not be indistinguishable just because both downloaded.
   * - Only what is committed or on its way is here. A `failed` row is a
   *   quarantine -- bytes the verifier refused -- so it belongs on the request
   *   book, not on the page that answers "what do I have?". The server decides
   *   that (`VISIBLE_STATES`); this view simply never has to draw one.
   */
  import AppMark from "$lib/components/AppMark.svelte";
  import AppShell from "$lib/components/nb/AppShell.svelte";
  import EmptyState from "$lib/components/nb/EmptyState.svelte";
  import { getView } from "$lib/api";
  import { filed, stateLabel, stateTone, windowOf } from "$lib/format";

  let data = $state(null);
  let failure = $state("");

  const IN_FLIGHT = new Set(["approved", "running"]);

  async function load(signal) {
    try {
      data = await getView("/ui/datasets.json", { signal });
      failure = "";
    } catch (error) {
      if (error.name !== "AbortError") failure = error.message;
    }
  }

  // Poll only while something is in flight: a static page that refreshes every
  // five seconds forever is a phone's battery paying for a page that cannot
  // change.
  $effect(() => {
    const controller = new AbortController();
    let timer = null;

    async function tick() {
      await load(controller.signal);
      const moving = (data?.totals?.in_flight ?? 0) > 0;
      if (moving && !controller.signal.aborted) timer = setTimeout(tick, 5000);
    }

    tick();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  });

  const totals = $derived(data?.totals ?? null);

  function size(bytes) {
    const value = Number(bytes ?? 0);
    if (!Number.isFinite(value) || value <= 0) return "—";
    const units = ["B", "kB", "MB", "GB", "TB"];
    let n = value;
    let i = 0;
    while (n >= 1000 && i < units.length - 1) {
      n /= 1000;
      i += 1;
    }
    return `${i === 0 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
  }

  // One helper for the whole app, so the boundary convention is said the same
  // way on the card as on the request page.
  const window_of = (request) => windowOf(request.start, request.end);

  function facts(request) {
    return [
      request.schema,
      (request.symbols ?? []).join(", "),
      request.stype_in,
    ]
      .filter(Boolean)
      .join(" · ");
  }
</script>

<AppShell
  footer={false}
  title="data-download"
  nav={[
    { label: "Requests", href: "/", current: false },
    { label: "Datasets", href: "/datasets", current: true },
    { label: "Plan types", href: "/plan-types", current: false },
  ]}
>
  {#snippet mark()}
    <AppMark />
  {/snippet}

  <p class="lede">Datasets</p>

  {#if failure}
    <EmptyState cause="error" customMessage={failure} />
  {:else if !data}
    <p class="hint">Looking at the datasets root…</p>
  {:else if !data.configured}
    <p class="empty">
      No datasets root is configured — the runner has nowhere to write, so
      nothing can land. Set <code>DATA_DOWNLOAD_DATASETS_DIR</code>.
    </p>
  {:else if !totals.requests}
    <p class="empty">
      Nothing has been run yet. A request appears here once a runner claims it.
    </p>
  {:else}
    <p class="summary">
      {totals.vendors} vendor{totals.vendors === 1 ? "" : "s"} ·
      {totals.datasets} dataset{totals.datasets === 1 ? "" : "s"} ·
      {totals.files} file{totals.files === 1 ? "" : "s"} ·
      {size(totals.bytes)}{#if totals.in_flight}
        · <span class="moving">{totals.in_flight} in flight</span>{/if}
    </p>

    {#each data.vendors as vendor (vendor.vendor_slug)}
      <section class="vendor">
        <h2>{vendor.vendor_slug || vendor.vendor}</h2>

        {#each vendor.datasets as group (group.dataset)}
          <h3>{group.dataset}</h3>

          {#each group.requests as request (request.request_id)}
            <article class="card">
              <header>
                <span class="pill {stateTone(request.state)}"
                  >{stateLabel(request.state)}</span
                >
                <a class="plan" href={`/requests/${request.request_id}`}
                  >{request.plan_type ?? "—"}</a
                >
              </header>

              {#if window_of(request) || facts(request)}
                <p class="facts">
                  {#if window_of(request)}<span class="when"
                      >{window_of(request)}</span
                    >{/if}
                  {#if facts(request)}<span class="what">{facts(request)}</span
                    >{/if}
                </p>
              {/if}

              {#if request.files.length}
                <ul class="files">
                  {#each request.files as file (file.name)}
                    <li>
                      {#if file.href}
                        <a href={file.href} download>{file.name}</a>
                      {:else}
                        <span>{file.name}</span>
                      {/if}
                      <span class="num">{size(file.bytes)}</span>
                    </li>
                  {/each}
                </ul>
              {:else if IN_FLIGHT.has(request.state)}
                <p class="quiet">No bytes on disk yet.</p>
              {:else if !request.on_disk}
                <p class="quiet">
                  This request settled {stateLabel(
                    request.state,
                  ).toLowerCase()}, but nothing is on disk — the directory is
                  gone.
                </p>
              {:else}
                <p class="quiet">Landed with no files.</p>
              {/if}

              <div class="card-foot">
                <span class="meta">
                  {#if request.settled_at}
                    landed {filed(request.settled_at)}
                  {:else}
                    filed {filed(request.created_at)}
                  {/if}
                  {#if request.landed_sha256}
                    · <code>sha {request.landed_sha256.slice(0, 12)}</code>
                  {/if}
                  {#if request.verdict}
                    · {request.verdict}
                  {/if}
                </span>
                <span class="total">{size(request.total_bytes)}</span>
              </div>
            </article>
          {/each}
        {/each}
      </section>
    {/each}
  {/if}
</AppShell>

<style>
  .lede {
    margin: 0 0 6px;
  }

  .summary {
    margin: 0 0 20px;
    color: var(--ink-muted);
    font-size: 13px;
  }

  .moving {
    color: var(--ink);
  }

  .vendor h2 {
    margin: 24px 0 4px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-muted);
  }

  .vendor h3 {
    margin: 14px 0 8px;
    font-family: var(--mono, ui-monospace, SFMono-Regular, monospace);
    font-size: 15px;
  }

  .card {
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 12px 14px;
    margin: 0 0 10px;
    background: var(--surface, transparent);
  }

  .card header {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  .plan {
    font-size: 13px;
    color: var(--ink-muted);
    text-decoration: none;
  }

  .facts {
    margin: 8px 0 0;
    display: flex;
    gap: 4px 10px;
    flex-wrap: wrap;
    font-size: 13px;
  }

  .when {
    font-variant-numeric: tabular-nums;
  }

  .what {
    color: var(--ink-muted);
  }

  .files {
    list-style: none;
    margin: 10px 0 0;
    padding: 0;
  }

  .files li {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 7px 0;
    border-top: 1px solid var(--line);
    font-size: 13px;
  }

  .files a {
    font-family: var(--mono, ui-monospace, SFMono-Regular, monospace);
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .num {
    font-variant-numeric: tabular-nums;
    color: var(--ink-muted);
    flex: none;
  }

  .quiet {
    margin: 10px 0 0;
    font-size: 13px;
    color: var(--ink-muted);
  }

  .card-foot {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    margin-top: 10px;
    padding-top: 8px;
    border-top: 1px solid var(--line);
    font-size: 12px;
    color: var(--ink-muted);
  }

  .meta code {
    font-size: 12px;
  }

  .empty {
    margin: 18px 0 0;
    color: var(--ink-muted);
    font-size: 13px;
  }
</style>
