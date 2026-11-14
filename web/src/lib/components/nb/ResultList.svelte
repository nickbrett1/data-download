<script>
  /**
   * ResultList — ranked entity list with a progressive fold.
   *
   * Generalised from stratechery-search: one row per article with title,
   * meta, status pill, and an optional matching snippet. Folds after
   * `initialCount` rows with an explicit disclosure button.
   *
   * Two additions in 1.4.0, both because a real result row is richer than
   * {href, title, pill, meta, snippet} and the alternative was to stop using
   * the block:
   *
   *   `row(item, index)` -- replace the body of a row entirely. A search hit
   *       carries more than one highlighted passage, a "N more passages" fold
   *       and per-passage metadata; none of that is a string field. The rank,
   *       the row chrome and the outer fold still come from the block.
   *   `titleTag` -- the title's element. Defaults to a `div` so existing output
   *       does not change, but a page that needs a section heading in its
   *       outline passes `"h2"`, instead of rendering its own heading and
   *       passing no title at all.
   */
  let {
    items = [],
    initialCount = 5,
    title = "",
    titleTag = "div",
    emptyMessage = "No results to display.",
    row,
  } = $props();

  let expanded = $state(false);

  const visible = $derived(expanded ? items : items.slice(0, initialCount));
  const remaining = $derived(Math.max(0, items.length - initialCount));
</script>

<div class="result-list-wrap">
  {#if title}
    <svelte:element this={titleTag} class="section-title">
      {title} ({items.length})
    </svelte:element>
  {/if}

  {#if items.length === 0}
    <div class="card empty-card hint">
      {emptyMessage}
    </div>
  {:else}
    <div class="card result-card">
      {#each visible as item, index}
        <div class="result-row" class:border-top={index > 0}>
          <div class="row-main">
            <span class="rank num">{index + 1}</span>
            {#if row}
              {@render row(item, index)}
            {:else}
              <div class="row-body">
                <div class="row-head">
                  {#if item.href}
                    <a href={item.href} class="row-title">{item.title}</a>
                  {:else}
                    <span class="row-title">{item.title}</span>
                  {/if}
                  {#if item.pill}
                    <span class="pill {item.pill.tone ?? ''}"
                      >{item.pill.label}</span
                    >
                  {/if}
                </div>
                {#if item.meta}
                  <div class="row-meta hint">{item.meta}</div>
                {/if}
                {#if item.snippet}
                  <div class="row-snippet">{item.snippet}</div>
                {/if}
              </div>
            {/if}
          </div>
        </div>
      {/each}
    </div>

    {#if remaining > 0}
      <div class="fold-row">
        <button
          type="button"
          class="btn fold-btn"
          onclick={() => (expanded = !expanded)}
        >
          {expanded ? "Show fewer" : `Show ${remaining} more…`}
        </button>
      </div>
    {/if}
  {/if}
</div>

<style>
  .result-list-wrap {
    margin-bottom: 24px;
  }

  .empty-card {
    padding: 16px;
    text-align: center;
  }

  .result-card {
    overflow: hidden;
  }

  .result-row {
    padding: 12px 14px;
  }

  .result-row.border-top {
    border-top: 1px solid var(--line);
  }

  .row-main {
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }

  .rank {
    font-size: 11px;
    color: var(--ink-faint);
    width: 20px;
    text-align: right;
    margin-top: 2px;
  }

  .row-body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .row-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 8px;
  }

  .row-title {
    font-size: 13.5px;
    font-weight: 500;
    color: var(--ink);
  }

  .row-meta {
    font-size: 11.5px;
  }

  .row-snippet {
    margin-top: 4px;
    font-size: 12px;
    line-height: 1.4;
    color: var(--ink-muted);
  }

  .fold-row {
    margin-top: 8px;
    display: flex;
    justify-content: center;
  }

  .fold-btn {
    width: 100%;
  }
</style>
