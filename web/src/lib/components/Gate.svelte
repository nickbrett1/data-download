<script>
  /**
   * Gate — the way in, when this browser has no token.
   *
   * The app used to carry its token in the URL (`?token=…`), which is why every
   * link was a credential: the Homepage tile's Docker label, the compose file,
   * access logs and browser history all got a copy. The shell is public now, so
   * a link is just a link and the token is something this browser holds.
   *
   * Two ways in, one screen:
   *
   *   - pasted here, once (`rememberToken`), for a browser that will come back;
   *   - `#token=…` on the address, which `api.token()` consumes and stores.
   *
   * A refused token lands here too, with `refused` set, because "the token is
   * wrong" and "there is no token" are the same next action and different
   * sentences.
   */
  let { refused = false, onunlock } = $props();

  let value = $state("");

  function submit(event) {
    event.preventDefault();
    if (!value.trim()) return;
    onunlock(value);
    value = "";
  }
</script>

<div class="card gate">
  <p class="micro">locked</p>
  <h2 class="gate-title">
    {refused ? "That token was refused" : "This browser has no read token"}
  </h2>
  <p class="hint">
    {refused
      ? "It may have been rotated, or copied short. Paste the current one."
      : "The pages themselves are public; the data behind them is not. Paste a token holding the read scope and it is kept in this browser."}
  </p>

  <form class="gate-form" onsubmit={submit}>
    <input
      type="password"
      name="token"
      bind:value
      placeholder="paste the read token"
      aria-label="read token"
      autocomplete="off"
      spellcheck="false"
    />
    <button class="btn primary" type="submit" disabled={!value.trim()}>
      Unlock
    </button>
  </form>

  <p class="hint gate-note">
    A link with <code>#token=…</code> works too — the fragment is never sent to
    the server, so it is the safe way to hand one over.
  </p>
</div>

<style>
  .gate {
    max-width: 30rem;
    margin: 40px auto;
    padding: 24px 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .gate-title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
    color: var(--ink);
  }
  .gate-form {
    display: flex;
    gap: 8px;
    margin-top: 4px;
  }
  .gate-form input {
    flex: 1;
    min-width: 0;
  }
  .gate-note {
    margin: 2px 0 0;
  }
</style>
