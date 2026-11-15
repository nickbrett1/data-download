/**
 * The one place the frontend talks to FastAPI.
 *
 * FastAPI owns the read-model: the four `v_*` SQL views, the facet precedence
 * (`broken` outranks the state it is stuck in) and the plan-type registry. This
 * app does not reimplement any of that; it fetches the same view-models the
 * Jinja templates used to be handed, and draws them.
 *
 * There is no credential here, by design. The pages and their view-models are a
 * public read-only projection -- there is no verb behind them -- so "can you
 * reach the port" is the whole trust model. The tokens that can *do* something
 * (`submit`, `escalate`) belong to machines and never come near a browser.
 * An attempt to put a token in a URL was removed for that reason: a URL is a
 * thing that gets copied into labels, logs and history.
 */

export async function getView(path, { signal } = {}) {
  const res = await fetch(path, { signal });
  if (!res.ok) {
    const error = new Error(`The server answered ${res.status}.`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}
