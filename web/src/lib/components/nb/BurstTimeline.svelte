<script>
  /**
   * BurstTimeline — interval chart + historical audit grid.
   *
   * Displays a bar chart of activity intervals followed by a history table with
   * comparison columns ("Explains"). Lifted from deepseek-balance and
   * data-download §8.
   *
   * Two additions in 1.4.0:
   *
   *   `orientation` -- `"vertical"` (hours down the day, as deepseek-balance
   *       draws it) or `"horizontal"` (one bar per bucket, labels down the left,
   *       as a year-by-year archive wants). The same data; only the axis moves.
   *   `chartLabel`  -- the caption over the chart. It was hard-coded to "recent
   *       bursts", so a caller with intervals that are not bursts had to choose
   *       between a wrong word and not using the block. Empty string hides it.
   *
   * And, in 1.5.0, the two things a *bar table* needs (which is a different
   * animal from the sparkline this started as):
   *
   *   `showValues` -- print each bucket's count at the end of its bar. Without
   *       it the number lived only in a `title` tooltip, so a page about "how
   *       big is this, by year" had to keep its own chart to show the figures.
   *   `caption`    -- a prose slot under the label. The caveat a chart carries
   *       ("daily updates reach this archive from March 2018, so the early bars
   *       are the weekly and free articles") is a sentence, not a 10px uppercase
   *       micro label, and it is the chart's own job to explain itself.
   */
  let {
    intervals = [],
    history = [],
    title = "Activity & History",
    chartLabel = "recent bursts",
    orientation = "vertical",
    showValues = false,
    caption,
  } = $props();

  const horizontal = $derived(orientation === "horizontal");

  const maxVal = $derived(
    intervals.reduce((m, x) => Math.max(m, x.count ?? 0), 1),
  );
</script>

<div class="timeline-wrap">
  <div class="section-title">{title}</div>

  <!-- Sparkline burst chart (SVG) -->
  {#if intervals.length > 0}
    <div class="card chart-card">
      {#if chartLabel}
        <div class="micro chart-title">{chartLabel}</div>
      {/if}
      {#if caption}
        <div class="chart-note hint">{@render caption()}</div>
      {/if}
      <div class="chart-bars" class:horizontal>
        {#each intervals as interval}
          {@const pct = Math.round(((interval.count ?? 0) / maxVal) * 100)}
          <div class="bar-col" title="{interval.label}: {interval.count}">
            <div class="bar-track">
              <div class="bar-fill" style="--pct: {pct}%;"></div>
            </div>
            <span class="bar-label num hint">{interval.label}</span>
            {#if showValues}
              <span class="bar-value num hint">{interval.count ?? 0}</span>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- History grid -->
  {#if history.length > 0}
    <div class="card history-card">
      <div class="history-head micro">
        <span>Timestamp</span>
        <span>Event</span>
        <span class="num-col">Value</span>
        <span>Explains</span>
      </div>
      {#each history as row, idx}
        <div class="history-row" class:border-top={idx > 0}>
          <span class="num hint">{row.date}</span>
          <span class="row-event">{row.label}</span>
          <span class="num num-col">{row.value}</span>
          <span class="row-explains hint">
            {#if row.diff}
              <code class="diff-chip">{row.diff}</code>
            {/if}
            {row.note ?? ""}
          </span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .timeline-wrap {
    margin-bottom: 24px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .chart-card {
    padding: 14px;
    background: var(--bg-sunken);
  }

  .chart-title {
    margin-bottom: 12px;
  }

  .chart-note {
    margin: -8px 0 10px;
  }

  .bar-value {
    font-size: 10px;
    flex: none;
  }

  .chart-bars {
    display: flex;
    align-items: flex-end;
    gap: 8px;
    height: 72px;
    padding-top: 8px;
  }

  .bar-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    height: 100%;
  }

  .bar-track {
    width: 100%;
    flex: 1;
    background: var(--line);
    border-radius: 3px;
    display: flex;
    align-items: flex-end;
    overflow: hidden;
  }

  /* The fill is driven by one custom property and the orientation decides which
     axis spends it, so there is no second copy of the chart markup. */
  .bar-fill {
    width: 100%;
    height: var(--pct, 0%);
    background: var(--accent);
    border-radius: 3px 3px 0 0;
    min-height: 2px;
    transition:
      height 0.2s ease,
      width 0.2s ease;
  }

  .chart-bars.horizontal {
    flex-direction: column;
    align-items: stretch;
    height: auto;
    gap: 6px;
  }

  /* An explicit grid rather than a reversed row: with three cells (label, bar,
     value) the bar has to be the flexible one and the numbers must line up
     down the list, which is a column definition, not a flex order. */
  .chart-bars.horizontal .bar-col {
    display: grid;
    grid-template-columns: 5rem minmax(0, 1fr) auto;
    align-items: center;
    gap: 10px;
    height: auto;
  }

  .chart-bars.horizontal .bar-label {
    grid-area: 1 / 1;
    text-align: right;
  }

  .chart-bars.horizontal .bar-track {
    grid-area: 1 / 2;
    height: 10px;
  }

  .chart-bars.horizontal .bar-value {
    grid-area: 1 / 3;
    text-align: right;
    min-width: 2.5rem;
  }

  .chart-bars.horizontal .bar-fill {
    width: var(--pct, 0%);
    height: 100%;
    border-radius: 0 3px 3px 0;
  }

  .bar-label {
    font-size: 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .history-card {
    overflow: hidden;
  }

  .history-head {
    display: grid;
    grid-template-columns: 140px 1fr 100px 1.5fr;
    gap: 12px;
    padding: 8px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--bg-sunken);
  }

  .history-row {
    display: grid;
    grid-template-columns: 140px 1fr 100px 1.5fr;
    gap: 12px;
    align-items: center;
    padding: 8px 12px;
    font-size: 12.5px;
  }

  .history-row.border-top {
    border-top: 1px solid var(--line);
  }

  .num-col {
    text-align: right;
  }

  .diff-chip {
    margin-right: 4px;
  }

  @media (max-width: 600px) {
    .history-head,
    .history-row {
      grid-template-columns: 1fr;
      gap: 4px;
    }

    .num-col {
      text-align: left;
    }
  }
</style>
