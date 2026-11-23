<script>
  /**
   * Take back a request nobody has approved yet.
   *
   * The case that asked for this: the retry proof left two identical drafts,
   * one of which was never wanted. There was no verb for "I don't want this
   * any more" — the choices were approving a request nobody wanted (a spend) or
   * editing the database by hand (a lie about the record).
   *
   * So this is not a delete. `POST /v1/requests/<id>/cancel` moves a `draft`
   * to `cancelled`: the row, its quote, its `why` and its events stay, and the
   * badge changes. A missing row would read as a request that was never filed.
   *
   * Two clicks before either one counts, and the second button is the one that
   * says the word. Nothing is spent either way — the point is not danger, it is
   * that cancelling is a statement about a filing, so it should take a second
   * look rather than a stray tap on a phone.
   */
  import { getView, postJson } from "$lib/api";

  let { requestId } = $props();

  let stage = $state("idle");
  let confirming = $state(false);
  let failure = $state("");
  let done = $state(null);

  // The door is retry's: a `submit` token or an attested approver. A browser
  // holds neither token nor secret, so the attested login is the only identity
  // this page can offer — ask the server whether this hop has one.
  let who = $state(null);
  let whoFailure = $state("");

  $effect(() => {
    const controller = new AbortController();
    getView("/v1/whoami", { signal: controller.signal })
      .then((body) => {
        who = body;
      })
      .catch((error) => {
        if (error.name !== "AbortError") whoFailure = error.message;
      });
    return () => controller.abort();
  });

  async function cancel() {
    stage = "busy";
    failure = "";
    try {
      done = await postJson(
        `/v1/requests/${encodeURIComponent(requestId)}/cancel`,
        {},
      );
    } catch (error) {
      failure = error.message;
    } finally {
      stage = "idle";
      confirming = false;
    }
  }
</script>

<section class="block cancel">
  <p class="micro">cancel</p>
  {#if done}
    <p class="quiet">
      Cancelled. Nothing was downloaded and nothing was spent, and the row stays
      here with its events — filing it again is a new request, not an undo.
    </p>
  {:else if whoFailure}
    <p class="failure">{whoFailure}</p>
  {:else if !who}
    <p class="quiet">Asking the server who this door thinks you are…</p>
  {:else}
    <p class="ask">
      Takes this request out of the queue for good: it will not be approved, it
      will not run, and nothing is spent. The row stays on this page — with its
      quote, its <em>why</em> and its events — marked <em>cancelled</em>.
    </p>
    {#if who.approver}
      <div class="row">
        {#if confirming}
          <button
            class="btn danger"
            type="button"
            disabled={stage === "busy"}
            onclick={cancel}
          >
            Yes, cancel it
          </button>
          <button
            class="btn"
            type="button"
            disabled={stage === "busy"}
            onclick={() => (confirming = false)}
          >
            Keep it
          </button>
        {:else}
          <button class="btn" type="button" onclick={() => (confirming = true)}>
            Cancel this request
          </button>
        {/if}
      </div>
    {:else}
      <p class="quiet">
        Cancelling as <strong>{who.login}</strong> is refused: that login may
        read but not file, because it is not named in
        <code>DATA_DOWNLOAD_APPROVERS</code>.
      </p>
    {/if}
  {/if}
  {#if failure}
    <p class="failure">{failure}</p>
  {/if}
</section>

<style>
  .block {
    margin: 0 0 20px;
  }

  .ask {
    margin: 8px 0 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink);
  }

  .row {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    margin-top: 10px;
  }

  .danger {
    border-color: var(--bad);
    color: var(--bad);
  }

  .failure {
    margin: 10px 0 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--bad);
  }
</style>
