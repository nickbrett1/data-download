<script>
  /**
   * Re-file a failed request, without touching the row that failed.
   *
   * The failure this card exists for is the one this deployment actually has:
   * `driver_not_ready` — the vendor is wired up, the driver is not installed, and
   * the repair happens in *another container*. Before this card the repair was
   * followed by hand-writing the same body again, because a `failed` row cannot
   * be approved or run: it is history, with events, an `errors_json` and (when
   * it got that far) an output directory that describe an attempt which really
   * happened.
   *
   * So this is not a rewind. It asks the server to file the same body again (see
   * `POST /v1/requests/<id>/retry`), which produces a **new** request — new id,
   * new quote, its own directory — and leaves it in `draft`. Somebody still
   * approves it, with the new number in front of them. Two clicks, and the
   * second one is still the one that spends.
   *
   * It is rendered only where a retry can help: the server refuses a row that
   * has not settled, and refuses a verifier's verdict (`verify_shortfall`,
   * `verify_contradiction`) because the same bytes will fail it again.
   * `isRetryable` mirrors that set, so the page never offers a verb the server
   * would refuse.
   */
  import { getView, postJson } from "$lib/api";

  let { requestId } = $props();

  let stage = $state("idle");
  let failure = $state("");
  let filed = $state(null);

  // The retry door takes a `submit` token or an attested approver; a browser
  // holds neither token nor secret, so this asks the same question the approve
  // card asks — is this hop a named tailnet login?
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

  async function retry() {
    stage = "busy";
    failure = "";
    try {
      filed = await postJson(
        `/v1/requests/${encodeURIComponent(requestId)}/retry`,
      );
    } catch (error) {
      failure = error.message;
    } finally {
      stage = "idle";
    }
  }
</script>

<section class="block retry">
  {#if filed}
    <p class="quiet">
      Filed as <a href={`/requests/${filed.request_id}`}
        ><code>{filed.request_id}</code></a
      >, quoting {filed.quote_usd === null || filed.quote_usd === undefined
        ? "nothing"
        : `$${Number(filed.quote_usd).toFixed(4)}`} — waiting for approval.
    </p>
  {:else if whoFailure}
    <p class="failure">{whoFailure}</p>
  {:else if who && !who.approver}
    <p class="quiet">
      Retrying as <strong>{who.login}</strong> is refused: not in
      <code>DATA_DOWNLOAD_APPROVERS</code>.
    </p>
  {:else}
    <button
      class="btn verb"
      type="button"
      disabled={stage === "busy" || !who}
      onclick={retry}
    >
      <!-- A circular arrow: the verb files the same body again. Decorative --
           the word beside it is the label. -->
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
  {#if failure}
    <p class="failure">{failure}</p>
  {/if}
</section>

<style>
  .failure {
    font-size: 12px;
    line-height: 1.5;
    color: var(--bad);
  }
</style>
