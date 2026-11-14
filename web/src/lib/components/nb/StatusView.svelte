<script>
  /**
   * StatusView — four-card status lanes.
   *
   * Displays status buckets (pending, done, stuck, blocked) as individual cards
   * with counts in the head and rows carrying pill badges. Lifted from
   * data-download §8.
   */
  let { cards = [] } = $props();
</script>

<div class="status-grid" style="--status-cols: {cards.length};">
  {#each cards as card}
    <div class="card status-card">
      <div class="card-head">
        <span class="micro card-title">{card.title}</span>
        <span class="pill {card.tone ?? ''} num">{card.count}</span>
      </div>

      <div class="card-rows">
        {#if card.rows.length === 0}
          <div class="empty-lane hint">No items</div>
        {:else}
          {#each card.rows as row, idx}
            <div class="lane-row" class:border-top={idx > 0}>
              <div class="row-info">
                {#if row.href}
                  <a href={row.href} class="row-title">{row.title}</a>
                {:else}
                  <span class="row-title">{row.title}</span>
                {/if}
                {#if row.meta}
                  <span class="row-meta hint">{row.meta}</span>
                {/if}
              </div>
              {#if row.pill}
                <span class="pill {row.pill.tone ?? ''}">{row.pill.label}</span>
              {/if}
            </div>
          {/each}
        {/if}
      </div>
    </div>
  {/each}
</div>

<style>
  .status-grid {
    display: grid;
    grid-template-columns: repeat(var(--status-cols, 4), 1fr);
    gap: 12px;
    align-items: start;
  }

  .status-card {
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .card-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
  }

  .card-title {
    margin: 0;
  }

  .card-rows {
    display: flex;
    flex-direction: column;
  }

  .empty-lane {
    padding: 16px 12px;
    text-align: center;
  }

  .lane-row {
    padding: 8px 12px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }

  .lane-row.border-top {
    border-top: 1px solid var(--line);
  }

  .row-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .row-title {
    font-size: 12.5px;
    color: var(--ink);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .row-meta {
    font-size: 11px;
  }

  @media (max-width: 900px) {
    .status-grid {
      grid-template-columns: repeat(2, 1fr);
    }
  }

  @media (max-width: 600px) {
    .status-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
