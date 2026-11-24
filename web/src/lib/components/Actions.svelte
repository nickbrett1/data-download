<script>
  /**
   * Every verb for one request, in one cluster.
   *
   * Four components used to render these -- approve as a card with a paragraph
   * above it, then cancelling, archiving and re-filing as three loose buttons in
   * a row -- which made the same kind of thing (a change to this request) look
   * like two different kinds of thing. Approve was visually the page's
   * headline; the other three were small print. They are one decision surface,
   * so they are one block: the question above the buttons, all four buttons in
   * one row, alphabetical and fixed (Approve, Archive, Cancel, Retry) so a row
   * you have seen once is a row you can hit blind.
   *
   * Merging them also removed four identical `/v1/whoami` requests. The page is
   * served by a process behind `tailscale serve`, which is the only party that
   * knows who is reading: every verb had to ask, and every verb got the same
   * answer. One question now, asked once, and the answer gates the whole row --
   * because all four doors are the same door (`auth.require_submit_or_approver`).
   *
   * What each verb means, and why each is shaped the way it is:
   *
   * - **Approve** is the one that spends. It asks the server (`/v1/whoami`),
   *   which is the only party that knows the identity -- the login is on the far
   *   side of a tunnel this page cannot see. One click: it used to be two
   *   ("Approve" then "Yes, approve"), and the second asked the question the
   *   numbers above the first had already answered. Both figures are on the page
   *   before the click: the vendor's quote for exactly this Job, and the ceiling
   *   the click authorises.
   *
   * - **Archive** writes a date rather than a state. A `failed` attempt's state
   *   is the outcome of something that really happened, so cancelling it would
   *   make the record lie; archiving only takes it off the book's "what needs
   *   me" list, and the same button puts it back. Archiving is also the one verb
   *   that ends the visit: the row is being put away, so `onarchived` hands the
   *   decision to leave the page back to whoever owns the route. Restoring does
   *   not, because you are asking for the row, not putting it away.
   *
   * - **Cancel** moves a `draft` to `cancelled` and is not a delete: the row,
   *   its quote, its `why` and its events stay. Two clicks, and the second says
   *   the word -- not because money is at risk, but because cancelling is a
   *   statement about a filing and deserves a second look rather than a stray
   *   tap.
   *
   * - **Retry** files the same body again as a *new* request and leaves it in
   *   `draft`. A failed row is history; the repair is a new filing, priced
   *   again, approved by a person again.
   *
   * The verbs a row offers are the server's rules, not this component's: the
   * page never offers a verb the server would refuse.
   */
  import { getView, postJson } from "$lib/api";
  import { bound, isArchivable, isRetryable, usd } from "$lib/format";

  let { request, onchanged, onarchived } = $props();

  const id = $derived(request.id);
  const draft = $derived(request.state === "draft");
  const archivable = $derived(isArchivable(request));
  const retryable = $derived(isRetryable(request));

  // `who` is null until the server answers. `whoFailure` is the refusal the
  // server sent when it could not tell -- a request that never came through
  // Serve. They are different states on purpose: "I do not know who you are" is
  // not "I know who you are and you may not change anything".
  let who = $state(null);
  let whoFailure = $state("");

  let note = $state("");
  // One guard for the whole row: a click disables every verb until the answer
  // arrives, so a double press cannot file or approve twice.
  let busy = $state("");
  // Per verb, so a refusal sits under the row instead of replacing it.
  let errors = $state({});
  let done = $state({});
  let confirming = $state(false);
  let filed = $state(null);
  let archiveResult = $state(null);

  $effect(() => {
    const controller = new AbortController();
    getView("/v1/whoami", { signal: controller.signal })
      .then((body) => {
        who = body;
      })
      .catch((error) => {
        if (error.name !== "AbortError") whoFailure = error.message;
      });
    forgetPastedToken();
    return () => controller.abort();
  });

  /** The token the approve card used to ask for is gone from the server, so a
   * copy sitting in this browser's storage is a live-looking secret with no
   * purpose. Delete it once, on the page that asked for it. */
  function forgetPastedToken() {
    try {
      window.localStorage.removeItem("data-download.approve-token");
      window.localStorage.removeItem("data-download.actor");
    } catch {
      /* a browser that refuses storage has nothing to forget */
    }
  }

  const ceiling =
    request.max_usd === null || request.max_usd === undefined
      ? null
      : bound(request.max_usd);
  const quote =
    request.estimate_usd === null || request.estimate_usd === undefined
      ? null
      : usd(request.estimate_usd);

  const archived = $derived(
    (archiveResult
      ? archiveResult.archived_at !== null
      : Boolean(request.archived_at)) === true,
  );

  // Nothing is clickable until the server has said this hop has a named login;
  // a `who` that never arrives is the `whoFailure` branch below, which offers
  // no verb at all rather than a button that cannot work.
  const ready = $derived(Boolean(who));

  async function run(verb, path, body) {
    busy = verb;
    errors = { ...errors, [verb]: "" };
    try {
      const result = await postJson(path, body);
      done = { ...done, [verb]: true };
      return result;
    } catch (error) {
      errors = { ...errors, [verb]: error.message };
      return null;
    } finally {
      busy = "";
      confirming = false;
    }
  }

  const base = $derived(`/v1/requests/${encodeURIComponent(id)}`);

  async function approve() {
    const result = await run("approve", `${base}/approve`, {
      note: note.trim() || null,
    });
    if (result) onchanged?.();
  }

  async function cancel() {
    const result = await run("cancel", `${base}/cancel`, {});
    if (result) onchanged?.();
  }

  async function retry() {
    filed = await run("retry", `${base}/retry`);
  }

  async function toggleArchive() {
    const result = await run("archive", `${base}/archive`, {
      archived: !archived,
    });
    if (!result) return;
    archiveResult = result;
    // Putting the row away ends the visit; asking for it back (a null
    // `archived_at`) does not -- you are still reading the row you asked for.
    if (result.archived_at) onarchived?.();
  }
</script>

<section class="block actions">
  <p class="micro">actions</p>

  {#if whoFailure}
    <!-- No identity, so there is no verb to offer: not a disabled button, which
         would promise that something here could be fixed by clicking. -->
    <p class="failure">{whoFailure}</p>
  {:else if who && !who.approver}
    <div class="approve">
      <p class="who">
        Changing a request as <strong>{who.login}</strong> is refused: not in
        <code>DATA_DOWNLOAD_APPROVERS</code>.
      </p>
    </div>
  {:else}
    {#if draft && !done.approve}
      <!-- The question the buttons answer: who is spending, what the vendor
           said it costs, what the click authorises, and the one free field the
           record keeps. Nothing prints until the page knows who is reading. -->
      <div class="approve">
        {#if who}
          <p class="who">Approving as <strong>{who.login}</strong>.</p>
        {/if}
        <p class="ask">
          {#if quote}Quoted <strong>{quote}</strong>.{/if}
          {#if ceiling}
            Approves up to <strong>{ceiling}</strong>.
          {:else}
            No ceiling — nothing here bounds the spend.
          {/if}
        </p>
        <div class="fields">
          <label>
            <span class="micro">note <span class="opt">(optional)</span></span>
            <input
              type="text"
              bind:value={note}
              placeholder="why this request is worth the money"
              autocomplete="off"
            />
          </label>
        </div>
      </div>
    {/if}

    <div class="verbs">
      {#if draft && !done.approve}
        <button
          class="btn verb primary"
          type="button"
          data-verb="approve"
          disabled={busy !== "" || !ready}
          onclick={approve}
        >
          Approve
        </button>
      {/if}
      {#if archivable}
        <button
          class="btn verb"
          type="button"
          data-verb="archive"
          disabled={busy !== "" || !ready}
          onclick={toggleArchive}
        >
          <!-- A box, with the arrow pointing the way this click moves the row:
               into it to archive, out of it to restore. Decorative -- the word
               beside it is the label. -->
          <svg
            class="glyph"
            viewBox="0 0 16 16"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="M2.6 5.6h10.8v7.8H2.6zM1.9 2.6h12.2v3H1.9z"
              fill="none"
              stroke="currentColor"
              stroke-width="1.4"
              stroke-linejoin="round"
            />
            {#if archived}
              <path
                d="M8 11.2V8.2M6.6 9.6L8 8.2l1.4 1.4"
                fill="none"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            {:else}
              <path
                d="M8 7.8v3M6.6 9.4L8 10.8l1.4-1.4"
                fill="none"
                stroke="currentColor"
                stroke-width="1.4"
                stroke-linecap="round"
                stroke-linejoin="round"
              />
            {/if}
          </svg>
          {archived ? "Restore" : "Archive"}
        </button>
      {/if}
      {#if draft && !done.cancel}
        <button
          class="btn verb danger"
          type="button"
          data-verb="cancel"
          disabled={busy !== "" || !ready}
          onclick={() => (confirming = true)}
        >
          <!-- A cross: cancelling takes the request off the queue. Decorative
               -- the word beside it is the label. -->
          <svg
            class="glyph"
            viewBox="0 0 16 16"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
            />
          </svg>
          Cancel
        </button>
      {/if}
      {#if retryable}
        <button
          class="btn verb"
          type="button"
          data-verb="retry"
          disabled={busy !== "" || !ready}
          onclick={retry}
        >
          <!-- A circular arrow: the verb files the same body again. Decorative
               -- the word beside it is the label. -->
          <svg
            class="glyph"
            viewBox="0 0 16 16"
            aria-hidden="true"
            focusable="false"
          >
            <path
              d="M12.8 8a4.8 4.8 0 1 1-1.5-3.5"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
            <path
              d="M13 2.4v3.2H9.8"
              fill="none"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          Retry
        </button>
      {/if}
    </div>

    {#if confirming}
      <!-- The second click, in the row beneath rather than beside: the buttons
           above stay where they were, so nothing jumps under a moving finger. -->
      <div class="verbs confirm">
        <button
          class="btn verb danger"
          type="button"
          data-verb="cancel-confirm"
          disabled={busy !== ""}
          onclick={cancel}
        >
          Yes, cancel
        </button>
        <button
          class="btn verb"
          type="button"
          disabled={busy !== ""}
          onclick={() => (confirming = false)}
        >
          Keep it
        </button>
      </div>
    {/if}

    {#if done.approve}
      <p class="quiet">Approved.</p>
    {/if}
    {#if done.cancel}
      <p class="quiet">Cancelled.</p>
    {/if}
    {#if filed}
      <p class="quiet">
        Filed as <a href={`/requests/${filed.request_id}`}
          ><code>{filed.request_id}</code></a
        >, quoting {filed.quote_usd === null || filed.quote_usd === undefined
          ? "nothing"
          : usd(filed.quote_usd)} — waiting for approval.
      </p>
    {/if}
    {#each Object.entries(errors).filter(([, text]) => text) as [verb, text] (verb)}
      <!-- The server's words, unedited: a refusal paraphrased is a guess. -->
      <p class="failure" data-verb={verb}>{text}</p>
    {/each}
  {/if}
</section>

<style>
  .block {
    margin: 0 0 20px;
  }

  .approve {
    display: grid;
    gap: 8px;
    margin: 0 0 12px;
  }

  .who,
  .ask {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink);
  }

  .fields label {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-width: 26rem;
  }

  .opt {
    color: var(--ink-faint);
  }

  /* One row of verbs. It wraps rather than shrinking when a message lands
     beside them -- "Filed as req_…" is a sentence, not a label. */
  .verbs {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
  }

  .verbs.confirm {
    margin-top: 10px;
  }

  /* Approve is the one that spends, so it keeps the accent even in a row where
     everything is a verb: the fill says "this is the expensive one" before the
     word is read. */
  .verbs [data-verb="approve"] {
    background: var(--accent-soft);
    border-color: var(--accent-line-strong);
    color: var(--accent);
  }

  .danger {
    border-color: var(--bad);
    color: var(--bad);
  }

  .quiet,
  .failure {
    margin: 10px 0 0;
    font-size: 12px;
    line-height: 1.5;
  }

  .failure {
    color: var(--bad);
  }
</style>
