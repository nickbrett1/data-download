<script>
  /**
   * FilterPanel — facet filter bar.
   *
   * Desktop: card panel with grouped checkboxes and counts.
   * Mobile (<600px): chip row that opens a slide-over / bottom sheet so
   * filters remain usable on small screens without taking over the document.
   *
   * `adaptFacets` (1.4.0): the panel still *renders* one shape --
   * `[{ key, label, options: [{ value, label, count }] }]` -- but an API rarely
   * emits it. stratechery-search's endpoint returns `by_year` / `by_kind` /
   * `by_entity` maps of counts, so adopting the block meant reshaping the
   * response in the page and throwing that away on every render. Hand the raw
   * payload to `facets` and a mapper to `adaptFacets`, and the panel owns the
   * conversion. Kept as a prop rather than a second accepted shape so the
   * contract stays one shape with one place that deviates from it.
   *
   * Three additions in 1.7.0, from the one screen that still could not use the
   * panel -- stratechery-search's year filter:
   *
   *   `facet.mode`   -- `"multiple"` (default, checkboxes) or `"single"`, which
   *       is one choice from N: a year, an "images / text only" toggle. Radios,
   *       and choosing the active option clears it, because a single-select
   *       facet with no way back is a trap.
   *   `facet.layout` -- `"list"` (default) or `"bars"`, which draws each
   *       option's `count` as a bar behind its label. A histogram is not a
   *       decoration on a year filter: it is how you see that the archive
   *       starts thin in 2018 before choosing a range.
   *   `facet.note`   -- one line under the group ("1,614 passages across two
   *       kinds"), because a count per option does not say what it counts.
   *
   * The shape stays `[{ key, label, options: [{ value, label, count }] }]`: the
   * three are facet fields with defaults, not new props.
   */
  let { facets = [], selected = {}, onchange, onclear, adaptFacets } = $props();

  const groups = $derived(adaptFacets ? adaptFacets(facets) : facets);

  let mobileOpen = $state(false);

  function isChecked(groupKey, value) {
    return (selected[groupKey] ?? []).includes(value);
  }

  // A single-select facet is one choice from N. Clicking the active one clears
  // it -- otherwise a range that matches nothing has no way back except "clear",
  // which throws away every other facet too.
  function choose(facet, value) {
    if (facet.mode !== "single") {
      toggle(facet.key, value);
      return;
    }
    const updated = { ...selected };
    if (isChecked(facet.key, value)) delete updated[facet.key];
    else updated[facet.key] = [value];
    onchange?.(updated);
  }

  // Bars are relative to the largest option in their own group: the question a
  // histogram answers is "how does this year compare with the others", not "how
  // many passages exist in total".
  function barWidth(facet, option) {
    const max = Math.max(1, ...facet.options.map((o) => o.count ?? 0));
    return `${Math.round(((option.count ?? 0) / max) * 100)}%`;
  }

  function toggle(groupKey, value) {
    const current = selected[groupKey] ?? [];
    const next = current.includes(value)
      ? current.filter((v) => v !== value)
      : [...current, value];
    const updated = { ...selected, [groupKey]: next };
    if (next.length === 0) delete updated[groupKey];
    onchange?.(updated);
  }

  const activeCount = $derived(
    Object.values(selected).reduce((acc, arr) => acc + (arr?.length ?? 0), 0),
  );
</script>

<!-- Mobile chip row (<600px) -->
<div class="mobile-chip-row">
  <button
    type="button"
    class="btn mobile-filter-btn"
    onclick={() => (mobileOpen = true)}
  >
    <span>Filters</span>
    {#if activeCount > 0}
      <span class="pill info">{activeCount}</span>
    {/if}
  </button>
  {#if activeCount > 0}
    <button type="button" class="btn clear-btn" onclick={() => onclear?.()}>
      Clear all
    </button>
  {/if}
</div>

<!-- Desktop sidebar panel / mobile bottom sheet -->
<div class="filter-panel" class:mobile-open={mobileOpen}>
  <div class="card panel-card">
    <div class="panel-header">
      <div class="panel-title-wrap">
        <span class="micro">Filters</span>
        {#if activeCount > 0}
          <span class="pill info">{activeCount}</span>
        {/if}
      </div>
      <div class="panel-actions">
        {#if activeCount > 0}
          <button
            type="button"
            class="btn text-btn"
            onclick={() => onclear?.()}
          >
            Clear
          </button>
        {/if}
        <button
          type="button"
          class="btn close-btn"
          onclick={() => (mobileOpen = false)}
          aria-label="Close filters"
        >
          ✕
        </button>
      </div>
    </div>

    {#each groups as facet}
      <div class="facet-group">
        <div class="facet-label micro">{facet.label}</div>
        <div class="facet-options" class:bars={facet.layout === "bars"}>
          {#each facet.options as opt}
            <label class="facet-option">
              {#if facet.layout === "bars"}
                <span
                  class="option-bar"
                  style="--w: {barWidth(facet, opt)}"
                  aria-hidden="true"
                ></span>
              {/if}
              <!-- A radio, not a checkbox with single-select behaviour bolted
                   on: the control should say what it does, and screen readers
                   announce the set as one group. -->
              <input
                type={facet.mode === "single" ? "radio" : "checkbox"}
                name={facet.mode === "single"
                  ? `nb-facet-${facet.key}`
                  : undefined}
                checked={isChecked(facet.key, opt.value)}
                onclick={() => choose(facet, opt.value)}
              />
              <span class="option-label">{opt.label}</span>
              {#if opt.count !== undefined}
                <span class="option-count num hint">{opt.count}</span>
              {/if}
            </label>
          {/each}
        </div>
        {#if facet.note}
          <p class="facet-note hint">{facet.note}</p>
        {/if}
      </div>
    {/each}
  </div>
</div>

{#if mobileOpen}
  <div
    class="backdrop"
    onclick={() => (mobileOpen = false)}
    role="presentation"
  ></div>
{/if}

<style>
  .mobile-chip-row {
    display: none;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
  }

  .panel-card {
    padding: 14px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .panel-title-wrap {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .panel-actions {
    display: flex;
    gap: 6px;
    align-items: center;
  }

  .close-btn {
    display: none;
    padding: 2px 8px;
  }

  .text-btn {
    padding: 2px 8px;
    font-size: 11px;
  }

  .facet-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .facet-label {
    margin: 0;
  }

  .facet-options {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .facet-option {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12.5px;
    cursor: pointer;
    color: var(--ink-dim);
    padding: 2px 0;
  }

  /* The histogram sits under the row rather than beside it, so label and count
     keep the same columns as every other facet. */
  .bars .facet-option {
    padding: 3px 5px;
    border-radius: 4px;
  }

  .option-bar {
    position: absolute;
    left: 0;
    top: 0;
    bottom: 0;
    width: var(--w);
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    border-radius: 4px;
  }

  .option-bar + input,
  .option-bar ~ .option-label,
  .option-bar ~ .option-count {
    position: relative;
  }

  .facet-note {
    margin: 0;
    font-size: 10.5px;
  }

  .facet-option:hover {
    color: var(--ink);
  }

  .option-label {
    flex: 1;
  }

  .option-count {
    font-size: 11px;
  }

  .backdrop {
    display: none;
  }

  @media (max-width: 600px) {
    .mobile-chip-row {
      display: flex;
    }

    .filter-panel {
      display: none;
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      z-index: 50;
      max-height: 80vh;
      overflow-y: auto;
    }

    .filter-panel.mobile-open {
      display: block;
    }

    .panel-card {
      border-radius: 12px 12px 0 0;
      border-bottom: 0;
      background: var(--bg);
    }

    .close-btn {
      display: inline-block;
    }

    .backdrop {
      display: block;
      position: fixed;
      inset: 0;
      background: color-mix(in srgb, var(--bg) 62%, transparent);
      z-index: 40;
    }
  }
</style>
