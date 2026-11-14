<script>
  /**
   * DetailPanel — entity inspection layout.
   *
   * Header with entity title + status pill, two-column metadata grid, and
   * prose body container. Includes a history-back button so the consumer
   * needs no navigation state. Lifted from stratechery-search and data-download.
   */
  let { title = "", pill, meta = [], onback, children } = $props();

  function handleBack() {
    if (onback) {
      onback();
    } else if (typeof window !== "undefined") {
      window.history.back();
    }
  }
</script>

<div class="detail-wrap">
  <div class="nav-bar">
    <button type="button" class="pill btn back-btn" onclick={handleBack}>
      ← back
    </button>
  </div>

  <article class="card detail-card">
    <header class="detail-header">
      <h1 class="detail-title">{title}</h1>
      {#if pill}
        <span class="pill {pill.tone ?? ''}">{pill.label}</span>
      {/if}
    </header>

    {#if meta.length > 0}
      <dl class="meta-grid">
        {#each meta as item}
          <div class="meta-cell">
            <dt class="micro">{item.label}</dt>
            <dd class="meta-val" class:num={item.isCode || item.isNum}>
              {#if item.isCode}
                <code>{item.value}</code>
              {:else}
                {item.value}
              {/if}
            </dd>
          </div>
        {/each}
      </dl>
    {/if}

    <div class="detail-body">
      {@render children?.()}
    </div>
  </article>
</div>

<style>
  .detail-wrap {
    max-width: 52rem;
    margin: 0 auto;
  }

  .nav-bar {
    margin-bottom: 12px;
  }

  .back-btn {
    cursor: pointer;
    background: transparent;
    border: 1px solid var(--line-strong);
    color: var(--ink-dim);
  }

  .detail-card {
    padding: 24px;
  }

  .detail-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--line);
  }

  .detail-title {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    letter-spacing: -0.01em;
    color: var(--ink);
    line-height: 1.3;
  }

  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px 16px;
    margin: 16px 0;
    padding: 0;
  }

  .meta-cell {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .meta-cell dt {
    margin: 0;
  }

  .meta-val {
    margin: 0;
    font-size: 13px;
    color: var(--ink-dim);
  }

  .detail-body {
    padding-top: 16px;
    border-top: 1px solid var(--line);
    font-size: 13.5px;
    line-height: 1.6;
    color: var(--ink);
  }

  @media (max-width: 600px) {
    .detail-card {
      padding: 16px;
    }

    .detail-header {
      flex-direction: column;
      gap: 8px;
    }
  }
</style>
