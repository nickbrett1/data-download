<script>
  /**
   * The one verb on the read surface: approve this request.
   *
   * §7.8.3 argued there should be no approve button, and the argument has been
   * overruled on purpose. It rested on a dashboard that opened the store
   * `mode=ro` in a container holding no credentials — a page that could not
   * have approved anything. That container was never built; the read surface is
   * now this app's own view-models, served by the process that owns the writer.
   * So the choice was never "safe page or unsafe page", it was "a route with a
   * scope check, or a `docker exec` with none".
   *
   * What was kept from that argument, because it was right:
   *   - the token is `approve`-scoped, is never granted by a bare token, and no
   *     agent holds one, so this is a human door even on a machine route;
   *   - the token travels in an `Authorization` header, never in a URL;
   *   - approving is two clicks, with the ceiling between them, so a mis-tap on
   *     a phone cannot spend anything;
   *   - the page still cannot do anything else. No edit, no cancel, no escalate.
   *
   * The token is kept in this browser's localStorage: one paste per device. It
   * is not fetched, not embedded, and never sent anywhere but this app's own
   * `/v1` route.
   */
  import { postJson } from "$lib/api";
  import { bound } from "$lib/format";

  let { requestId, maxUsd = null, onapproved } = $props();

  const TOKEN_KEY = "data-download.approve-token";
  const ACTOR_KEY = "data-download.actor";

  function read(key) {
    try {
      return window.localStorage.getItem(key) ?? "";
    } catch {
      // A browser that refuses storage still gets a working form; the token
      // just has to be pasted again next time.
      return "";
    }
  }

  function write(key, value) {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch {
      /* storage is a convenience, not a requirement */
    }
  }

  let token = $state("");
  let actor = $state("");
  let note = $state("");
  // idle -> confirm -> busy; `confirm` exists so the amount is read before the
  // commit, and `busy` so a double-click cannot post twice.
  let stage = $state("idle");
  let failure = $state("");
  let done = $state(false);

  $effect(() => {
    token = read(TOKEN_KEY);
    actor = read(ACTOR_KEY);
  });

  const ceiling =
    maxUsd === null || maxUsd === undefined ? null : bound(maxUsd);
  const ready = $derived(token.trim().length > 0 && actor.trim().length > 0);

  function saveCredentials() {
    write(TOKEN_KEY, token.trim());
    write(ACTOR_KEY, actor.trim());
  }

  async function approve() {
    stage = "busy";
    failure = "";
    try {
      saveCredentials();
      await postJson(
        `/v1/requests/${encodeURIComponent(requestId)}/approve`,
        { actor: actor.trim(), note: note.trim() || null },
        { token: token.trim() },
      );
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
  {:else}
    <div class="fields">
      <label>
        <span class="micro">approving token</span>
        <input
          type="password"
          bind:value={token}
          placeholder="paste the approve token"
          autocomplete="off"
          spellcheck="false"
        />
      </label>
      <label>
        <span class="micro">as</span>
        <input
          type="text"
          bind:value={actor}
          placeholder="your name"
          autocomplete="off"
          spellcheck="false"
        />
      </label>
      <label class="wide">
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
        disabled={!ready || stage === "busy"}
        onclick={() => {
          failure = "";
          stage = "confirm";
        }}
      >
        Approve{ceiling ? ` up to ${ceiling}` : ""}
      </button>
      {#if !ready}
        <span class="quiet hint"
          >Needs the approve token and a name: an approval records who asked for
          the spend.</span
        >
      {/if}
    </div>
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

  .fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin: 8px 0 12px;
  }

  .fields label {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .fields .wide {
    grid-column: 1 / -1;
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

  .hint {
    font-size: 11px;
    max-width: 34ch;
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

  @media (max-width: 620px) {
    .fields {
      grid-template-columns: 1fr;
    }
  }
</style>
