"""The driver loop's authoring harness: a ticket in, a branch and a sha out.

The outside contract has not moved. `driver_build` runs a command with the
ticket on stdin and reads one JSON line back:

    {"branch": "driver-loop/databento-20261125t041233z", "commit_sha": "abc123…"}

What moved is *who does the git*. The loop gates everything by sha through
`github_api` -- checks, changed paths, the merge, and the tarball it installs
from -- so the checkout this harness used to keep on the host was never
evidence, only somewhere for an agent to type. It is gone: the authoring agent
now clones the (public) repository into its own workspace, works on a branch
name **this harness chose**, commits, and pushes. What comes back is the
forge's answer to "what does that branch point at", which is a stronger
statement than a local `rev-parse`: the sha the loop gates is, by construction,
the sha GitHub reports for the branch it names.

The branch name is still the harness's fact, and so is the pull request: the
agent is told not to open one, and `merge` finds a PR by head branch, so the
mapping from branch to review artefact must not depend on an agent's manners.

The credential is the one thing the agent does need, and it is *narrower* than
what this harness used to hold. Today it is written into the agent's workspace
as a 0600 file for the duration of the run and deleted afterwards; the loop's
own token is passed because no repo-scoped key exists yet. Replacing it with a
write deploy key on the drivers repo is a change to one variable
(`GITHUB_TOKEN`) and no code.

Exit codes, unchanged: 0 answered, 1 refused (reason on stderr), 2 misconfigured.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from . import github_api

DEFAULT_REPO = "nickbrett1/data-download-drivers"
DEFAULT_BASE = "main"
DEFAULT_TIMEOUT = 3600.0
DEFAULT_WORKDIR = "/projects/driver-loop"

#: The ticket's first line, which names the vendor. Kept out of the prompt on
#: purpose: an agent that has to parse its own instructions is a second parser.
VENDOR_LINE = re.compile(r"^Write a driver for the vendor '?(?P<vendor>[^'.]+)'?\.")


class Refused(Exception):
    """This job stops here, and a human is told why.

    `output` carries the agent's stdout when there was one, so a run that failed
    *after* naming its transcript still leaves the transcript id behind.
    """

    output: str = ""


class Misconfigured(Refused):
    """Nothing to retry: the deployment is missing something."""


def vendor_of(ticket: str) -> str:
    first = (ticket.strip().splitlines() or [""])[0]
    match = VENDOR_LINE.match(first)
    return (match.group("vendor").strip() if match else "") or "unknown"


def slug(value: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return lowered or "unknown"


def branch_for(vendor: str, *, now: datetime | None = None) -> str:
    """A branch this harness made, so a ticket can never answer with an old one."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dt%H%M%Sz")
    return f"driver-loop/{slug(vendor)}-{stamp}"


def prompt_for(
    ticket: str,
    *,
    repo: str,
    branch: str,
    base: str,
    workdir: str,
    credential: str | None,
) -> str:
    """What the agent is told. Short, and specific about the four things that
    decide whether the work is usable: where, which branch, how to push, and
    which paths are allowed."""
    lines = [
        ticket.strip(),
        "",
        "How to deliver it",
        "-----------------",
        f"The repository is public: git clone https://github.com/{repo} {workdir}/drivers",
        f"(reuse that directory if it already exists). Create the branch {branch}",
        f"from {base} and do the work there.",
        "",
        "Commit your work on that branch and push it before you finish. Nothing",
        "is collected from this workspace -- an unpushed commit does not exist:",
        "",
        f"    git push origin HEAD:refs/heads/{branch}",
        "",
    ]
    if credential:
        lines += [
            f"A push credential is in the file {credential} (mode 0600). Read it at",
            "push time, do not print it, do not copy it into the repository, a",
            "commit or your final message:",
            "",
            f"    git push https://x-access-token:$(cat {credential})@github.com/{repo}"
            f" HEAD:refs/heads/{branch}",
            "",
        ]
    lines += [
        f"Do not push to {base} and do not open a pull request: the harness opens",
        "the PR and the loop decides whether the branch is merged.",
        "",
        "Change only paths under drivers/ or tests/fixtures/. Anything else is",
        "refused before the driver is even probed, so a branch that edits src/ or",
        "the CI configuration wastes the attempt.",
        "",
        f"When the branch is pushed, your last message is one line: pushed {branch}",
    ]
    return "\n".join(lines)


def _container(container: str) -> list[str]:
    return ["docker", "exec", "-i", container]


def write_credential(container: str, path: str, value: str) -> None:
    """Put a file in the agent's workspace without a mounted volume.

    `docker exec` runs as the container's own user (uid 10001), which owns the
    workspace, so no host root is involved. `umask 077` is the point: the file
    is readable by the agent and by nobody else.
    """
    script = 'umask 077; mkdir -p "$(dirname "$1")" && cat > "$1"'
    try:
        done = subprocess.run(  # noqa: S603 - docker exec, an operator's own host
            [*_container(container), "sh", "-c", script, "sh", path],
            input=value,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refused(f"cannot write the push credential into {container}: {exc}") from None
    if done.returncode != 0:
        raise Refused(
            "cannot write the push credential into "
            f"{container}:{(done.stderr or '').strip()[:200]}"
        )


def remove_credential(container: str, path: str) -> None:
    """Best effort, and deliberately quiet: a run that failed for another reason
    must not be reported as a failure to clean up."""
    try:
        subprocess.run(  # noqa: S603 - docker exec, an operator's own host
            [*_container(container), "sh", "-c", 'rm -f "$1"', "sh", path],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def sweep_credentials(container: str, prefix: str, *, older_than: float = 0.0) -> int:
    """Delete credentials a previous run could not, because it was killed.

    `finally` does not run on SIGKILL, so the cleanup above is a promise the
    process can break. Every run therefore starts by clearing the debris of the
    last one.

    The age guard is what keeps that from reaching into a live run: a file
    younger than the authoring timeout may still belong to a job in flight, so
    only stale ones go. In the loop, which runs one job at a time, that means
    the file is cleared by the run after next rather than never.
    """
    where = prefix.rstrip("/") or "/"
    minutes = max(1, int(older_than / 60))
    script = (
        'find "$1" -maxdepth 2 -name push-credential -mmin +"$2" '
        "-print -delete 2>/dev/null | wc -l"
    )
    try:
        done = subprocess.run(  # noqa: S603 - docker exec, an operator's own host
            [*_container(container), "sh", "-c", script, "sh", where, str(minutes)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if done.returncode != 0:
        return 0
    try:
        return int((done.stdout or "0").strip() or 0)
    except ValueError:
        return 0


def run_agent(cmd: str, prompt: str, *, timeout: float) -> str:
    """Run the authoring agent, and answer with its stdout.

    `shlex.split` rather than a shell: the command is one variable an operator
    sets, and the prompt is data that must never be re-parsed as syntax.
    """
    try:
        parts = shlex.split(cmd)
    except ValueError as exc:
        raise Misconfigured(f"AUTHOR_AGENT_CMD cannot be parsed: {exc}") from None
    if not parts:
        raise Misconfigured("AUTHOR_AGENT_CMD is empty")
    try:
        done = subprocess.run(  # noqa: S603 - an operator-supplied command
            [*parts, prompt],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Refused(f"the authoring agent did not finish within {int(timeout)}s") from None
    except OSError as exc:
        raise Misconfigured(f"the authoring agent could not be started: {exc}") from None
    if done.stderr.strip():
        print(done.stderr.rstrip(), file=sys.stderr)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-800:]
        refused = Refused(f"the authoring agent exited {done.returncode}: {tail}")
        refused.output = done.stdout
        raise refused
    return done.stdout


def author_ref_of(output: str) -> str:
    """The transcript id, if the agent is one that keeps one.

    Absent for a plain shell agent (`goose run`), which is not an error: the
    reference is provenance, not part of the contract.
    """
    for line in output.splitlines():
        if line.startswith("author-ref:"):
            return line.split(":", 1)[1].strip()
    return ""


def verify(repo: str, branch: str, base: str, *, token: str) -> dict[str, Any]:
    """What did the agent actually push? Ask the forge, not the agent."""
    if not token:
        raise Misconfigured("no GITHUB_TOKEN/GH_TOKEN; the branch cannot be verified or merged")
    try:
        head = github_api.head_sha(repo, branch, token=token)
    except github_api.GithubError as exc:
        raise Refused(f"nothing was pushed to {branch!r} on {repo}: {exc}") from None
    base_sha = github_api.head_sha(repo, base, token=token)
    if head == base_sha:
        raise Refused(
            f"the branch {branch!r} is still at {base}, so the agent committed nothing"
        )
    return {"commit_sha": head, "base_sha": base_sha}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author one driver from one ticket.")
    parser.add_argument("--repo", default=(os.environ.get("DRIVERS_REPO") or DEFAULT_REPO))
    parser.add_argument("--base", default=(os.environ.get("BASE_BRANCH") or DEFAULT_BASE))
    parser.add_argument(
        "--agent-cmd",
        default=(os.environ.get("AUTHOR_AGENT_CMD") or "").strip()
        or f"{sys.executable} -m data_download.openhands_agent",
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("AUTHOR_TIMEOUT") or DEFAULT_TIMEOUT)
    )
    parser.add_argument("--branch", default="", help="defaults to driver-loop/<vendor>-<stamp>")
    parser.add_argument(
        "--workdir-prefix",
        default=(os.environ.get("OPENHANDS_WORKDIR") or DEFAULT_WORKDIR).strip(),
    )
    parser.add_argument(
        "--container", default=(os.environ.get("OPENHANDS_CONTAINER") or "openhands").strip()
    )
    args = parser.parse_args(argv)

    ticket = sys.stdin.read()
    if not ticket.strip():
        print("driver-author: REFUSED: no ticket on stdin", file=sys.stderr)
        return 1

    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    vendor = vendor_of(ticket)
    branch = args.branch or branch_for(vendor)
    workdir = f"{args.workdir_prefix.rstrip('/')}/{slug(branch)}"
    credential = f"{workdir}/push-credential" if token else None

    print(f"driver-author: vendor {vendor!r}, branch {branch}, workspace {workdir}", file=sys.stderr)

    author_ref = ""
    failure: Refused | None = None
    try:
        if credential:
            stale = sweep_credentials(args.container, args.workdir_prefix, older_than=args.timeout)
            if stale:
                print(
                    f"driver-author: cleared {stale} credential(s) left by a killed run",
                    file=sys.stderr,
                )
            write_credential(args.container, credential, token)
        else:
            print(
                "driver-author: no GITHUB_TOKEN; the agent is expected to push with "
                "credentials it already has",
                file=sys.stderr,
            )
        prompt = prompt_for(
            ticket,
            repo=args.repo,
            branch=branch,
            base=args.base,
            workdir=workdir,
            credential=credential,
        )
        try:
            output = run_agent(args.agent_cmd, prompt, timeout=args.timeout)
        except Misconfigured:
            raise
        except Refused as exc:
            # The agent's own verdict is not the last word, the branch is. An
            # agent can push and *then* die -- a tool-schema blip at the end, a
            # timeout during its closing summary -- and the work is still real.
            # So the failure is held, not raised, and the forge decides.
            failure = exc
            output = exc.output
        author_ref = author_ref_of(output)
        try:
            answer = verify(args.repo, branch, args.base, token=token)
        except Refused:
            if failure is not None:
                raise failure from None
            raise
        if failure is not None:
            print(
                f"driver-author: the agent reported failure ({failure}), but the forge has "
                f"{answer['commit_sha']} on {branch}; trusting the forge",
                file=sys.stderr,
            )
    except Misconfigured as exc:
        print(f"driver-author: MISCONFIGURED: {exc}", file=sys.stderr)
        return 2
    except Refused as exc:
        print(f"driver-author: REFUSED: {exc}", file=sys.stderr)
        return 1
    finally:
        if credential:
            remove_credential(args.container, credential)

    try:
        pull = github_api.ensure_pull_request(
            args.repo,
            head=branch,
            base=args.base,
            title=f"driver: {vendor}",
            body=(
                f"Authored by the driver loop for the vendor {vendor}.\n\n"
                "The loop gates this branch's checks, probes it against the vendor, "
                "and merges it only if every changed path is under `drivers/` or "
                "`tests/fixtures/`."
                + (f"\n\nAuthored in: {author_ref}" if author_ref else "")
            ),
            token=token,
        )
    except github_api.GithubError as exc:
        # The commit exists and is what the loop gates; a missing PR only stops
        # the *merge*, so this is a refusal with the reason rather than a crash.
        print(f"driver-author: REFUSED: could not open a pull request: {exc}", file=sys.stderr)
        return 1
    print(
        f"driver-author: {answer['commit_sha']} on {branch} "
        f"(PR #{pull.get('number')}{'' if pull.get('created') else ', already open'})",
        file=sys.stderr,
    )

    line: dict[str, Any] = {"branch": branch, "commit_sha": answer["commit_sha"]}
    if author_ref:
        line["author_ref"] = author_ref
    print(json.dumps(line))
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
