"""The one place the driver loop talks to GitHub.

`tests/test_store_and_egress.py` allows `urllib.request` in this module and
nowhere else, for the same reason `runtime_http.py` owns `http.client`: an
egress path should be one file that a reader can audit, not a grep over a tree.
The loop is the only part of this service that has business with a forge -- it
merges code -- so it is also the only part that holds this credential.

Everything here returns a plain dict and raises `GithubError` with the sentence
GitHub gave, because a loop that stops must say why in the job's own words.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

#: Conclusions that count as green. `neutral` and `skipped` are checks that
#: chose not to object; anything else (failure, cancelled, timed_out, action
#: required) is a stop.
GREEN = frozenset({"success", "neutral", "skipped"})


class GithubError(Exception):
    """GitHub refused, or answered something the loop cannot read."""


def _call(
    method: str,
    path: str,
    *,
    token: str,
    body: dict | None = None,
    raw: bool = False,
    timeout: float = 60.0,
) -> object:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "data-download-driver-loop",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - api.github.com
            payload = answer.read()
            if raw:
                return payload
            return json.loads(payload.decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise GithubError(f"{method} {path} answered {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise GithubError(f"{method} {path} could not reach GitHub: {exc.reason}") from None


def gate(repo: str, sha: str, *, token: str) -> dict:
    """Are this commit's check runs green?

    Deliberately *all* runs, not a named one: a pipeline that renames its steps
    should not be able to slip past the gate, and a commit with no checks at all
    is not green -- it is untested, which is the same answer.
    """
    answer = _call("GET", f"/repos/{repo}/commits/{sha}/check-runs", token=token)
    runs = (answer or {}).get("check_runs") or []
    if not runs:
        return {"ok": False, "detail": "no check runs for this commit"}
    pending = [r.get("name") for r in runs if r.get("status") != "completed"]
    if pending:
        return {"ok": False, "detail": "still running: " + ", ".join(map(str, pending[:5]))}
    bad = [
        f"{r.get('name')}: {r.get('conclusion')}"
        for r in runs
        if (r.get("conclusion") or "") not in GREEN
    ]
    if bad:
        return {"ok": False, "detail": "; ".join(bad[:5])}
    return {"ok": True, "detail": f"{len(runs)} check run(s) green"}


def changed_paths(repo: str, sha: str, *, token: str) -> list[str]:
    """Every file the commit touched, from GitHub's own diff."""
    answer = _call("GET", f"/repos/{repo}/commits/{sha}", token=token)
    files = (answer or {}).get("files")
    if files is None:
        raise GithubError("the commit answer carried no file list")
    return [str(item.get("filename") or "") for item in files if item.get("filename")]


def merge(repo: str, branch: str, *, token: str) -> dict:
    """Merge the open PR whose head is `branch`.

    A branch with no PR is a harness that did not open one; the loop does not
    invent a PR because the PR is the review artefact a human reads later.
    """
    owner = repo.split("/", 1)[0]
    listed = _call(
        "GET",
        f"/repos/{repo}/pulls?state=open&head={urllib.parse.quote(f'{owner}:{branch}')}",
        token=token,
    )
    if not isinstance(listed, list) or not listed:
        raise GithubError(f"no open pull request for branch {branch!r}")
    number = listed[0]["number"]
    answer = _call(
        "PUT",
        f"/repos/{repo}/pulls/{number}/merge",
        token=token,
        body={"merge_method": "squash"},
    )
    if not (answer or {}).get("merged"):
        raise GithubError(f"the pull request {number} did not merge: {answer}")
    return {"number": number, "sha": (answer or {}).get("sha")}


def tarball(repo: str, sha: str, *, token: str) -> bytes:
    """The commit as a gzipped tarball -- what the loop installs from."""
    payload = _call("GET", f"/repos/{repo}/tarball/{sha}", token=token, raw=True)
    if not isinstance(payload, (bytes, bytearray)) or not payload:
        raise GithubError("the tarball answer was empty")
    return bytes(payload)
