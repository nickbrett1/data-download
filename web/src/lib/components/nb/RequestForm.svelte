<script>
  /**
   * RequestForm — schema-driven input form with constraint error presentation.
   *
   * Renders input controls from field definitions (not hand-coded HTML).
   * Displays per-field constraint violations (one message per failed rule)
   * using the house .notice.bad styling. Lifted from data-download §7.8.
   */
  let {
    title = "",
    fields = [],
    values = $bindable({}),
    errors = {},
    submitLabel = "Submit",
    onsubmit,
    submitting = false,
  } = $props();

  function handleSubmit(event) {
    event.preventDefault();
    onsubmit?.(values);
  }
</script>

<form class="card form-card" onsubmit={handleSubmit}>
  {#if title}
    <div class="card-head">
      <span class="micro">{title}</span>
    </div>
  {/if}

  <div class="fields">
    {#each fields as field}
      {@const fieldErrors = errors[field.name] ?? []}
      <div class="field-row">
        <label for="field-{field.name}" class="field-label">
          <span class="label-text">{field.label}</span>
          {#if field.required}
            <span class="required micro">required</span>
          {/if}
        </label>

        {#if field.type === "checkbox"}
          <input
            id="field-{field.name}"
            type="checkbox"
            bind:checked={values[field.name]}
          />
        {:else if field.type === "select"}
          <select
            id="field-{field.name}"
            class="input-select"
            bind:value={values[field.name]}
          >
            {#each field.options ?? [] as opt}
              <option value={opt.value}>{opt.label}</option>
            {/each}
          </select>
        {:else if field.type === "number"}
          <input
            id="field-{field.name}"
            type="number"
            class="input-text"
            placeholder={field.placeholder ?? ""}
            bind:value={values[field.name]}
          />
        {:else}
          <input
            id="field-{field.name}"
            type="text"
            class="input-text"
            placeholder={field.placeholder ?? ""}
            bind:value={values[field.name]}
          />
        {/if}

        {#if field.hint}
          <span class="hint">{field.hint}</span>
        {/if}

        <!-- Constraint errors: one message per failed rule -->
        {#if fieldErrors.length > 0}
          <div class="field-errors">
            {#each fieldErrors as err}
              <div class="notice bad">{err}</div>
            {/each}
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="form-actions">
    <button type="submit" class="btn primary" disabled={submitting}>
      {submitting ? "Submitting…" : submitLabel}
    </button>
  </div>
</form>

<style>
  .form-card {
    padding: 20px;
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 44rem;
  }

  .card-head {
    padding-bottom: 12px;
    border-bottom: 1px solid var(--line);
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .field-row {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field-label {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 12.5px;
    font-weight: 500;
    color: var(--ink-dim);
  }

  .required {
    font-size: 9px;
    color: var(--bad);
  }

  .input-text,
  .input-select {
    background: var(--bg-sunken);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    color: var(--ink);
    font: inherit;
    font-size: 13.5px;
    padding: 7px 10px;
    width: 100%;
  }

  .input-select {
    cursor: pointer;
  }

  .field-errors {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 4px;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: 12px;
    border-top: 1px solid var(--line);
  }
</style>
