/**
 * The one place the frontend talks to FastAPI.
 *
 * FastAPI owns the read-model: the four `v_*` SQL views, the facet precedence
 * (`broken` outranks the state it is stuck in) and the plan-type registry. This
 * app does not reimplement any of that; it fetches the same view-models the
 * Jinja templates used to be handed, and draws them.
 *
 * A token never travels in a URL. The server refuses `?token=` outright, and
 * the HTML shell is public, so a plain link, a Homepage tile or a home-screen
 * icon opens the app with no credential in it. The token arrives one of two
 * ways and then stays in `localStorage`:
 *
 *   `#token=…`  a fragment. Never sent to the server, never in an access log,
 *               never in a `Referer` -- the only URL shape worth pasting.
 *               Consumed on first read and stripped from the address bar.
 *   the gate    pasted once into the unlock card, which is what a phone gets.
 *
 * From then on every request carries it in an `Authorization` header, so the
 * only place it can leak is this browser.
 */

const KEY = "data_download.read_token";

/* A fragment, not a query: `#token=abc`. Anchors (`#plan_type`) share the
   slot, so an entry only counts when it parses as `token=`. */
function tokenFromHash() {
  if (typeof window === "undefined") return "";
  const hash = window.location.hash.replace(/^#/, "");
  if (!hash) return "";
  const token = new URLSearchParams(hash).get("token");
  return token ? token.trim() : "";
}

function stripHash() {
  window.history.replaceState(
    {},
    "",
    window.location.pathname + window.location.search,
  );
}

export function token() {
  if (typeof window === "undefined") return "";
  const fromHash = tokenFromHash();
  if (fromHash) {
    // Hand it over and take it back out of the address bar, so the next
    // screenshot, bookmark or forwarded link does not carry it.
    try {
      window.localStorage.setItem(KEY, fromHash);
    } catch {
      /* private mode: the token lives for this page view only */
    }
    stripHash();
    return fromHash;
  }
  try {
    return window.localStorage.getItem(KEY) ?? "";
  } catch {
    return "";
  }
}

export function rememberToken(value) {
  const trimmed = (value ?? "").trim();
  if (!trimmed) return "";
  try {
    window.localStorage.setItem(KEY, trimmed);
  } catch {
    /* private mode: still usable for this page view */
  }
  return trimmed;
}

export function forgetToken() {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* nothing to forget */
  }
}

export async function getView(path, { signal } = {}) {
  const bearer = token();
  const res = await fetch(path, {
    headers: bearer ? { Authorization: `Bearer ${bearer}` } : {},
    signal,
  });
  if (res.status === 401 || res.status === 403) {
    // The token is wrong, expired, or was never supplied. The gate is the
    // answer in every case, and the app decides that -- not this module.
    forgetToken();
    if (typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("auth:needed"));
    }
    const error = new Error("This page is locked.");
    error.status = res.status;
    error.auth = true;
    throw error;
  }
  if (!res.ok) {
    const error = new Error(`The server answered ${res.status}.`);
    error.status = res.status;
    throw error;
  }
  return res.json();
}
