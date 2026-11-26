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
 * spends money, so it needs the login `tailscale serve` asserts on the loopback
 * hop. There is no token to send and nothing this module could add: the
 * credential is the hop itself, so `postJson` has no auth option at all. A
 * header written here would be a second, self-declared identity -- which the
 * route refuses.
 */

/** The server's own sentence about a refusal, and the code to branch on.
 *
 * One envelope, one reader: `{"error": {"code", "message"}}` is what every
 * route returns, and its message is written for a person ("not named in
 * DATA_DOWNLOAD_APPROVERS", "no Tailscale-User-Login…"). Inventing a generic
 * string here would throw away the only part worth showing. */
async function refusal(res) {
  const payload = await res.json().catch(() => null);
  return {
    message: payload?.error?.message ?? `The server answered ${res.status}.`,
    code: payload?.error?.code ?? null,
  };
}

export async function getView(path, { signal } = {}) {
  const res = await fetch(path, { signal });
  if (!res.ok) {
    const { message, code } = await refusal(res);
    const error = new Error(message);
    error.status = res.status;
    error.code = code;
    throw error;
  }
  return res.json();
}

export async function postJson(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // Every call from here is a person's click on a page, and the retry route
      // files a new row -- so the row it creates should say so. The other verbs
      // ignore it; the submit path is the only reader.
      "X-Data-Download-Filed-By": "page",
    },
    body: JSON.stringify(body ?? {}),
  });
  if (!res.ok) {
    const { message, code } = await refusal(res);
    const error = new Error(message);
    error.status = res.status;
    error.code = code;
    throw error;
  }
  return res.json();
}
