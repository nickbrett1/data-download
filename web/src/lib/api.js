/**
 * The one place the frontend talks to FastAPI.
 *
 * FastAPI owns the read-model: the four `v_*` SQL views, the facet precedence
 * (`broken` outranks the state it is stuck in) and the plan-type registry. This
 * app does not reimplement any of that; it fetches the same view-models the
 * Jinja templates used to be handed, and draws them.
 *
 * Reading takes no credential, by design: the pages and their view-models are a
 * public read-only projection -- there is no verb behind them -- so "can you
 * reach the port" is the whole trust model for reading.
 *
 * Writing is one thing, and it is not reading. `POST /v1/requests/{id}/approve`
 * spends money, so it needs the `approve` token in an `Authorization` header.
 * It is never in a URL: a URL is a thing that gets copied into labels, logs and
 * history, and that is exactly how a credential leaked the last time one was
 * put in one.
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

export async function postJson(path, body, { token } = {}) {
  const res = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body ?? {}),
  });
  // The error envelope is the server's, and its message is the useful part:
  // `insufficient_scope` tells you the token cannot approve, not that it 403'd.
  const payload = await res.json().catch(() => null);
  if (!res.ok) {
    const error = new Error(
      payload?.error?.message ?? `The server answered ${res.status}.`,
    );
    error.status = res.status;
    error.code = payload?.error?.code ?? null;
    throw error;
  }
  return payload;
}
