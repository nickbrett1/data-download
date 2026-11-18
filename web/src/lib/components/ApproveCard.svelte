<script>
  /**
   * The one verb on the read surface: approve this request.
   *
   * §7.8.3 argued there should be no approve button, and the argument has been
   * overruled on purpose. It rested on a dashboard that opened the store
   * `mode=ro` in a container holding no credentials — a page that could not
   * have approved anything. That container was never built; the read surface is
   * now this app's own view-models, served by the process that owns the writer.
   * So the choice was never "safe page or unsafe page", it was "a route with an
   * identity check, or a `docker exec` with none".
   *
   * The check has been through two shapes. It was an `approve`-scoped token
   * pasted into this card; it is now the login `tailscale serve` asserts on the
   * loopback hop, which the server checks against `DATA_DOWNLOAD_APPROVERS`.
   * What that buys: the card cannot be talked into a spend by being pasted a
   * secret, and a page in some other tab has no way to arrive from Serve's hop.
   *
   * What survived from the token version, because it was right: approving is
   * two clicks with the ceiling between them, so a mis-tap on a phone cannot
   * spend anything; and the page still cannot do anything else.
   *
   * This card no longer asks who you are, because it does not have to. It asks
   * the server (`/v1/whoami`), which is the only party that knows: the identity
   * is on the far side of a tunnel this page cannot see.
   */
  import { getView, postJson } from "$lib/api";
  import { bound } from "$lib/format";

  let { requestId, maxUsd = null, onapproved } = $props();

  let note = $state("");
  // idle -> confirm -> busy; `confirm` exists so the amount is read before the
  // commit, and `busy` so a double-click cannot post twice.
  let stage = $state("idle");
  let failure = $state("");
  let done = $state(false);

  // `who` is null until the server answers. `whoFailure` is the refusal the
  // server sent when it could not tell — a request that never came through
  // Serve. They are different states on purpose: "I do not know who you are" is
  // not "I know who you are and you may not spend".
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
    forgetPastedToken();
    return () => controller.abort();
  });

  /** The token this card used to ask for is gone from the server, so a copy
   * sitting in this browser's storage is a live-looking secret with no purpose.
   * Delete it once, on the page that asked for it. */
  function forgetPastedToken() {
    try {
      window.localStorage.removeItem("data-download.approve-token");
      window.localStorage.removeItem("data-download.actor");
    } catch {
      /* a browser that refuses storage has nothing to forget */
    }
  }

  const ceiling =
    maxUsd === null || maxUsd === undefined ? null : bound(maxUsd);
  const ready = $derived(who?.approver === true);

  async function approve() {
    stage = "busy";
    failure = "";
    try {
      await postJson(`/v1/requests/${encodeURIComponent(requestId)}/approve`, {
        note: note.trim() || null,
      });
      done = true;
      onapproved?.();
    } catch (error) {
      failure = error.message;
      stage = "idle";
    }
  }
</script>

<section class="block approve">
  <p class="micro">spend</p>
  {#if done}
    <p class="quiet">
      Approved. The record has moved to <code>approved</code> and a
      <code>human</code> event is on the trail; nothing is fetched by this page.
    </p>
  {:else if stage === "confirm"}
    <!-- The number and the window, then one button. The ceiling is what is
         being authorised: the quote is a later step, so this says "up to". -->
    <p class="ask">
      Approve this request up to <strong
        >{ceiling ?? "the ceiling it carries"}</strong
      >? Nothing is downloaded here — a runner picks up an approved row.
    </p>
    {#if !ceiling}
      <p class="quiet note">
        This request carries no <code>max_usd</code>, so the approval is not
        bounded by a number this page can show you.
      </p>
    {/if}
    <div class="row">
      <button class="btn primary" type="button" onclick={approve}>
        Yes, approve {ceiling ?? "it"}
      </button>
      <button
        class="btn"
        type="button"
        onclick={() => {
          stage = "idle";
        }}
      >
        Cancel
      </button>
    </div>
  {:else if whoFailure}
    <!-- No identity, so there is no verb to offer: not a disabled button, which
         would promise that something here could be fixed by clicking. -->
    <p class="failure">{whoFailure}</p>
  {:else if !who}
    <p class="quiet">Asking the server who this door thinks you are…</p>
  {:else}
    <p class="who">
      Approving as <strong>{who.login}</strong>.
      {#if !who.approver}
        <span class="quiet"
          >That login may read but not approve: it is not named in
          <code>DATA_DOWNLOAD_APPROVERS</code>.</span
        >
      {/if}
    </p>
    {#if ready}
      <div class="fields">
        <label>
          <span class="micro">note <span class="opt">(optional)</span></span>
          <input
            type="text"
            bind:value={note}
            placeholder="why this window is worth the money"
            autocomplete="off"
          />
        </label>
      </div>
      <div class="row">
        <button
          class="btn primary"
          type="button"
          disabled={stage === "busy"}
          onclick={() => {
            failure = "";
            stage = "confirm";
          }}
        >
          Approve{ceiling ? ` up to ${ceiling}` : ""}
        </button>
      </div>
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

  .who {
    margin: 8px 0 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink);
  }

  .fields {
    display: grid;
    grid-template-columns: 1fr;
    gap: 10px;
    margin: 8px 0 12px;
  }

  .fields label {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .fields input {
    background: var(--bg-sunken);
    border: 1px solid var(--line-strong);
    border-radius: var(--radius-sm);
    color: var(--ink);
    font: inherit;
    font-size: 12px;
    padding: 5px 8px;
  }

  .fields input:focus {
    outline: 1px solid var(--accent-line);
  }

  .opt {
    color: var(--ink-faint);
  }

  .row {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
  }

  .note {
    margin: 8px 0 0;
    font-size: 12px;
    line-height: 1.5;
  }

  .failure {
    margin: 10px 0 0;
    font-size: 12px;
    color: var(--bad);
  }
</style>
