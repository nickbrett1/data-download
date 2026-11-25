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
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import github_api, openhands_agent

DEFAULT_REPO = "nickbrett1/data-download-drivers"
DEFAULT_BASE = "main"
DEFAULT_TIMEOUT = 3600.0
DEFAULT_WORKDIR = "/projects/driver-loop"
DEFAULT_CONTAINER = "openhands"
#: Where this process sees the agent's `/projects`, when it can see it at all.
#: Empty means "no shared filesystem", and the credential goes in with `docker
#: exec` -- the host-run deployment's way. In the control plane it is a mount.
DEFAULT_PROJECTS_DIR = ""

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


class Workspace:
    """Where the push credential lives for the duration of one run.

    Two implementations, because there are two ways a control plane can reach
    the agent's files: a shared directory it mounts itself, or a container it
    shells `docker exec` into. Everything above this is the same code, and the
    difference is one setting (`OPENHANDS_PROJECTS_DIR`).
    """

    def write(self, path: str, value: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def remove(self, path: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def sweep(self, prefix: str, *, older_than: float) -> int:  # pragma: no cover
        raise NotImplementedError


class SharedWorkspace(Workspace):
    """The agent's `/projects`, mounted into this process at the **same path**.

    Same path on both sides is the invariant that makes this a file write
    instead of a protocol: the prompt says `/projects/driver-loop/...`, the
    agent sees exactly that, and so does this process. A deployment that mounts
    the volume somewhere else is refused by name rather than handed a credential
    the agent cannot read.
    """

    def __init__(self, root: Path, *, owner: tuple[int, int] | None = None) -> None:
        self.root = Path(root)
        # The app image declares no USER, so it writes as root; the agent is a
        # different uid in a different container. A 0600 root-owned credential
        # is therefore a credential the agent cannot read, and a 0700 root-owned
        # directory is one it cannot enter -- the failure would arrive as a
        # silently credential-less agent, not as an error. When this process is
        # root and the deployment names the agent's uid, both are handed over.
        # A host run is already the same user, so `None` changes nothing.
        self.owner = owner if owner and os.geteuid() == 0 else None

    def _claim(self, where: Path) -> None:
        """Create `where` and any missing parents so the agent can traverse them."""
        missing: list[Path] = []
        probe = where
        while not probe.exists() and probe != probe.parent:
            missing.append(probe)
            probe = probe.parent
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            os.chmod(directory, 0o755)
            if self.owner:
                os.chown(directory, *self.owner)

    def _under_root(self, path: str) -> Path:
        target = Path(path)
        try:
            target.relative_to(self.root)
        except ValueError:
            raise Misconfigured(
                f"the credential path {path} is not under {self.root}: mount the "
                "agent's projects volume here at the path the agent sees"
            ) from None
        return target

    def write(self, path: str, value: str) -> None:
        target = self._under_root(path)
        try:
            self._claim(target.parent)
            # 0600 from the moment it exists: `open(…, 0o600)` cannot race a
            # default umask the way `write_text` followed by `chmod` can.
            handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                file.write(value)
            if self.owner:
                os.chown(target, *self.owner)
        except OSError as exc:
            raise Refused(f"cannot write the push credential to {path}: {exc}") from None

    def remove(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    def sweep(self, prefix: str, *, older_than: float = 0.0) -> int:
        """The same age guard as the docker version: never touch a live run."""
        where = Path(prefix)
        if not where.is_dir():
            return 0
        cutoff = time.time() - max(60.0, older_than)
        cleared = 0
        for candidate in where.glob("*/push-credential"):
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
                    cleared += 1
            except OSError:
                continue
        return cleared


class ContainerWorkspace(Workspace):
    """`docker exec` into the agent's container (a host-run deployment)."""

    def __init__(self, container: str) -> None:
        self.container = container

    def write(self, path: str, value: str) -> None:
        write_credential(self.container, path, value)

    def remove(self, path: str) -> None:
        remove_credential(self.container, path)

    def sweep(self, prefix: str, *, older_than: float = 0.0) -> int:
        return sweep_credentials(self.container, prefix, older_than=older_than)


def workspace_for(
    container: str = DEFAULT_CONTAINER,
    projects_dir: str = "",
    *,
    owner: tuple[int, int] | None = None,
) -> Workspace:
    if projects_dir:
        return SharedWorkspace(Path(projects_dir), owner=owner)
    return ContainerWorkspace(container)


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


def author(
    ticket: str,
    *,
    repo: str = DEFAULT_REPO,
    base: str = DEFAULT_BASE,
    token: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    workdir_prefix: str = DEFAULT_WORKDIR,
    container: str = DEFAULT_CONTAINER,
    projects_dir: str = DEFAULT_PROJECTS_DIR,
    owner: tuple[int, int] | None = None,
    agent_cmd: str = "",
    branch: str = "",
    url: str = "",
    budget: float = openhands_agent.DEFAULT_BUDGET,
    iterations: int = openhands_agent.DEFAULT_ITERATIONS,
    now: datetime | None = None,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One ticket, one branch, one pull request. The whole harness as one call.

    Answers `{"branch", "commit_sha"[,"author_ref"]}` -- the same dict the CLI
    prints as one JSON line -- so the pipeline can hold it in-process instead of
    spawning a process and parsing its stdout. The CLI is now a wrapper over
    this, which is why the two cannot drift.

    `agent_cmd` empty means "talk to the agent server directly" (see
    `openhands_agent.converse`); set it to run an agent as a child process
    instead. Either way the forge is what decides whether the work happened.
    """
    say = say or (lambda line: print(f"driver-author: {line}", file=sys.stderr))
    url = (
        url
        or (os.environ.get("DATA_DOWNLOAD_OPENHANDS_URL") or "").strip()
        or (os.environ.get("OPENHANDS_URL") or "").strip()
        or openhands_agent.DEFAULT_URL
    )

    vendor = vendor_of(ticket)
    chosen = branch or branch_for(vendor, now=now)
    # A directory per branch: two runs never share a workspace, and a branch
    # that was never pushed leaves nothing behind that the next run can reuse.
    workdir = f"{workdir_prefix.rstrip('/')}/{slug(chosen)}"
    credential = f"{workdir}/push-credential" if token else None
    workspace = workspace_for(container, projects_dir, owner=owner)

    say(f"vendor {vendor!r}, branch {chosen}, workspace {workdir}")

    author_ref = ""
    failure: Refused | None = None
    try:
        if credential:
            stale = workspace.sweep(workdir_prefix, older_than=timeout)
            if stale:
                say(f"cleared {stale} credential(s) left by a killed run")
            workspace.write(credential, token)
        else:
            # An agent that carries its own git credentials needs no file from
            # us. Rare, and it still has to survive `verify` against the forge.
            say("no GITHUB_TOKEN; the agent is expected to push with credentials it already has")
        prompt = prompt_for(
            ticket,
            repo=repo,
            branch=chosen,
            base=base,
            workdir=workdir,
            credential=credential,
        )
        try:
            if agent_cmd:
                author_ref = author_ref_of(run_agent(agent_cmd, prompt, timeout=timeout))
            else:
                answer = openhands_agent.converse(
                    prompt,
                    url=url,
                    workdir=workdir,
                    budget=budget,
                    iterations=iterations,
                    tags={"vendor": slug(vendor), "branch": slug(chosen)},
                    timeout=timeout,
                    say=say,
                )
                author_ref = str(answer["conversation"])
        except Misconfigured:
            raise
        except Refused as exc:
            # The agent's own verdict is not the last word, the branch is. An
            # agent can push and *then* die -- a tool-schema blip at the end, a
            # timeout during its closing summary -- and the work is still real.
            # So the failure is held, not raised, and the forge decides.
            failure = exc
            author_ref = author_ref or author_ref_of(exc.output)
        except openhands_agent.AgentConfig as exc:
            raise Misconfigured(str(exc)) from None
        except openhands_agent.AgentError as exc:
            failure = Refused(str(exc))
            author_ref = author_ref or exc.conversation
        try:
            answer = verify(repo, chosen, base, token=token)
        except Refused:
            # `Misconfigured` is a `Refused`: a missing token is not more
            # interesting than the agent's own reason for stopping.
            if failure is not None:
                raise failure from None
            raise
        if failure is not None:
            say(
                f"the agent reported failure ({failure}), but the forge has "
                f"{answer['commit_sha']} on {chosen}; trusting the forge"
            )
    except (Misconfigured, Refused):
        raise
    finally:
        if credential:
            workspace.remove(credential)

    try:
        pull = github_api.ensure_pull_request(
            repo,
            head=chosen,
            base=base,
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
        raise Refused(f"could not open a pull request: {exc}") from None
    say(
        f"{answer['commit_sha']} on {chosen} "
        f"(PR #{pull.get('number')}{'' if pull.get('created') else ', already open'})"
    )

    line: dict[str, Any] = {"branch": chosen, "commit_sha": answer["commit_sha"]}
    if author_ref:
        line["author_ref"] = author_ref
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Author one driver from one ticket.")
    parser.add_argument("--repo", default=(os.environ.get("DRIVERS_REPO") or DEFAULT_REPO))
    parser.add_argument("--base", default=(os.environ.get("BASE_BRANCH") or DEFAULT_BASE))
    parser.add_argument(
        "--agent-cmd",
        default=(os.environ.get("AUTHOR_AGENT_CMD") or "").strip(),
        help="run this command instead of talking to the agent server",
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
        "--container", default=(os.environ.get("OPENHANDS_CONTAINER") or DEFAULT_CONTAINER).strip()
    )
    parser.add_argument(
        "--projects-dir",
        default=(os.environ.get("OPENHANDS_PROJECTS_DIR") or DEFAULT_PROJECTS_DIR).strip(),
        help="the agent's /projects as this process sees it; empty means docker exec",
    )
    args = parser.parse_args(argv)

    ticket = sys.stdin.read()
    if not ticket.strip():
        print("driver-author: REFUSED: no ticket on stdin", file=sys.stderr)
        return 1

    try:
        answer = author(
            ticket,
            repo=args.repo,
            base=args.base,
            token=(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip(),
            timeout=args.timeout,
            workdir_prefix=args.workdir_prefix,
            container=args.container,
            projects_dir=args.projects_dir,
            agent_cmd=args.agent_cmd,
            branch=args.branch,
        )
    except Misconfigured as exc:
        print(f"driver-author: MISCONFIGURED: {exc}", file=sys.stderr)
        return 2
    except Refused as exc:
        print(f"driver-author: REFUSED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(answer))
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
