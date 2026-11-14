/**
 * The one place the frontend talks to FastAPI.
 *
 * FastAPI owns the read-model: the four `v_*` SQL views, the facet precedence
 * (`broken` outranks the state it is stuck in) and the plan-type registry. This
 * app does not reimplement any of that; it fetches the same view-models the
 * Jinja templates used to be handed, and draws them.
 *
 * The token travels in an Authorization header, not the query string. The old
 * pages put `?token=` in every link because a browser navigation cannot set a
 * header -- that is still true for links between the two pages (`withToken`),
 * but an XHR can, so the API call leaves it out of the URL and out of the logs.
 */

export function pageToken() {
  if (typeof window === "undefined") return "";
  return new URLSearchParams(window.location.search).get("token") ?? "";
}

export function withToken(href) {
  const token = pageToken();
  if (!token) return href;
  const sep = href.includes("?") ? "&" : "?";
  return `${href}${sep}token=${encodeURIComponent(token)}`;
}

export async function getView(path, { signal } = {}) {
  const token = pageToken();
  const res = await fetch(path, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    signal,
  });
  if (!res.ok) {
    const error = new Error(
      res.status === 401 || res.status === 403
        ? "Not authorized — open this page with ?token=…"
        : `The server answered ${res.status}.`,
    );
    error.status = res.status;
    throw error;
  }
  return res.json();
}
