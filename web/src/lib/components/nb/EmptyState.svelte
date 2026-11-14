<script>
  /**
   * EmptyState — cause-aware zero-result presentation.
   *
   * Differentiates "no results because filters are too narrow" (shows a Clear
   * Filters button) from "no results for search phrase" (suggests fewer words),
   * and generic empty states. Lifted from stratechery-search.
   */
  let { cause = "empty", query = "", onclear, customMessage = "" } = $props();
</script>

<div class="card empty-state">
  <div class="empty-icon micro">zero results</div>

  <h2 class="empty-title">
    {#if customMessage}
      {customMessage}
    {:else if cause === "filters"}
      No matches for active filters
    {:else if cause === "query"}
      No results found for <mark>{query}</mark>
    {:else if cause === "error"}
      Unable to load data
    {:else}
      Nothing to display
    {/if}
  </h2>

  <p class="empty-hint hint">
    {#if cause === "filters"}
      Try clearing one or more filters to broaden your search.
    {:else if cause === "query"}
      Check for typos, or try using fewer or more general keywords.
    {:else if cause === "error"}
      An unexpected error occurred while fetching data. Try refreshing the page.
    {:else}
      No items have been added to this view yet.
    {/if}
  </p>

  {#if cause === "filters" && onclear}
    <div class="empty-action">
      <button type="button" class="btn primary" onclick={() => onclear()}>
        Clear all filters
      </button>
    </div>
  {/if}
</div>

<style>
  .empty-state {
    padding: 32px 20px;
    text-align: center;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    margin: 20px 0;
  }

  .empty-icon {
    letter-spacing: 0.1em;
  }

  .empty-title {
    margin: 4px 0 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--ink);
  }

  .empty-hint {
    margin: 0;
    max-width: 32rem;
    line-height: 1.5;
  }

  .empty-action {
    margin-top: 12px;
  }
</style>
