"""The one place this process opens an HTTP connection to the driver runtime.

Phase 5's egress gate (`tests/test_store_and_egress.py`) bans `http.client`
everywhere under `src/` because a second import is a second way out of the
container. This module is the single exception, and it is deliberately dull:
stdlib `http.client`, one connection per call, no pooling, no retries, no
redirects, no TLS.

Three containment rules, all enforced on the *host string* before a socket is
opened, because that is the only part a misconfiguration can get wrong:

1. **No scheme, no port, no credentials in the host.** `http://x` or
   `user:pw@x` is a URL pasted into a host field; refusing it is clearer than
   letting `HTTPConnection` fail somewhere less obvious.
2. **Loopback or a single-label name.** `127.0.0.1`, `::1` and `localhost` are
   fine; anything else must be a bare compose service name (`data-download-
   drivers`). A dotted name is a public FQDN, and reaching one would make this
   process an egress path in its own right -- exactly what the split exists to
   prevent.
3. **A timeout on every call.** There is no "wait forever" option here; the
   caller passes its verb's deadline and this module honours it.

`request()` returns `(status, payload)`. It does **not** raise for a non-2xx:
the driver runtime's error bodies carry a machine-readable `code`, and turning
those into exceptions here would put the vocabulary in two places. The client
above this module decides what a `409` means.
"""

from __future__ import annotations

import http.client
import json
import re
from typing import Any

#: A compose service name: one label, lowercase, no dots. Dots are the thing
#: that would let this reach the internet, so they are refused rather than
#: warned about.
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")

LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

#: Bodies are small (verb arguments and file lists). A cap means a hostile or
#: broken runtime cannot fill this process's memory by answering with a stream.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class RuntimeUnreachable(Exception):
    """The runtime could not be reached, or answered something that is not JSON.

    Distinct from an error *from* the runtime: that arrives as a status and a
    body, and is the caller's business. This is "there was no conversation".
    """


def check_host(host: str) -> str:
    """Validate the host string. Returns it, so call sites can be one expression."""
    if not host:
        raise RuntimeUnreachable("no driver runtime host configured")
    if host in LOOPBACK:
        return host
    if not SERVICE_NAME.match(host):
        raise RuntimeUnreachable(
            f"driver runtime host {host!r} is neither loopback nor a single compose "
            "service name; a dotted name would make this process an egress path"
        )
    return host


def request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    """One HTTP request, one connection, no retry. `(status, parsed body)`.

    `token` is required and positional to the caller's attention: a call site
    that can omit the credential is a call site that eventually will.
    """
    if not token:
        raise RuntimeUnreachable(
            "no driver runtime token: set DRIVER_RUNTIME_TOKEN. An unauthenticated "
            "call is refused here rather than sent, so a missing setting fails "
            "loudly instead of relying on the runtime to be strict."
        )
    if not path.startswith("/"):
        raise RuntimeUnreachable(f"path {path!r} must start with '/'")
    check_host(host)

    payload = None if body is None else json.dumps(body, default=str).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))

    conn = http.client.HTTPConnection(host, int(port), timeout=timeout)
    try:
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES)
        status = response.status
    except (OSError, http.client.HTTPException) as exc:
        # Includes socket timeouts, connection refused, and a server that hangs
        # up mid-body. All of them mean the same thing to the caller: the verb
        # did not run, or its answer is lost.
        raise RuntimeUnreachable(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
    finally:
        conn.close()

    if not raw:
        return status, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeUnreachable(
            f"{method} {path}: answered {status} with something that is not JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeUnreachable(f"{method} {path}: JSON body is not an object")
    return status, parsed
