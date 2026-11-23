<script>
  /**
   * Archive a finished request — or put it back.
   *
   * A `failed` row used to have one verb, retry, and no way to stop looking at
   * it. Cancel is not that verb: it writes `state`, and a failed attempt's
   * state is the outcome of something that really happened — the vendor was
   * asked, the driver refused, the errors are on the row. Marking that
   * `cancelled` would make the record claim the request was cancelled before it
   * ran.
   *
   * So this writes a date instead. The row keeps its badge, its errors and its
   * events, and leaves the book's default list — which is a list of "what needs
   * me", and a finished row nobody is looking at is noise next to one that does.
   * It stays reachable under the book's `Archived` filter, and this card turns
   * into the way back, because a page that can hide a row without being able to
   * bring it back is a page that loses things.
   *
   * One click rather than two: unlike cancelling, nothing about it is
   * irreversible, and the button that undoes it is where the first one was.
   */
  import { getView, postJson } from "$lib/api";

  let { requestId, archivedAt } = $props();

  let stage = $state("idle");
  let failure = $state("");
  let result = $state(null);

  // Same door as retry and cancel: a `submit` token or an attested approver.
  // The page holds no token, so the login is what it can offer.
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

  const archived = $derived(
    (result ? result.archived_at !== null : Boolean(archivedAt)) === true,
  );

  async function toggle() {
    stage = "busy";
    failure = "";
    try {
      result = await postJson(
        `/v1/requests/${encodeURIComponent(requestId)}/archive`,
        { archived: !archived },
      );
    } catch (error) {
      failure = error.message;
    } finally {
      stage = "idle";
    }
  }
</script>

<section class="block archive">
  {#if whoFailure}
    <p class="failure">{whoFailure}</p>
  {:else if who && !who.approver}
    <p class="quiet">
      Archiving as <strong>{who.login}</strong> is refused: not in
      <code>DATA_DOWNLOAD_APPROVERS</code>.
    </p>
  {:else}
    <button
      class="btn"
      type="button"
      disabled={stage === "busy" || !who}
      onclick={toggle}
    >
      {archived ? "Put it back in the book" : "Archive this request"}
    </button>
  {/if}
  {#if failure}
    <p class="failure">{failure}</p>
  {/if}
</section>

<style>
  .failure {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--bad);
  }
</style>
