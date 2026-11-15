"""Which commit is about to spend money (§4.1).

`code_ref` is the git sha of the code that produced a Job. Git supplies it for
free, which is why §4.1 needs a rule and not a subsystem -- but "free" holds
only where a checkout exists, and the deployment is an image. So there are two
sources and they are resolved once per process:

1. `DATA_DOWNLOAD_CODE_REF` -- the image's build arg
   (`org.opencontainers.image.revision`, the commit that built the image);
2. `git rev-parse HEAD` -- a dev container with a checkout, where the answer is
   the working tree's commit.

Neither available is `None`, and that is a recorded fact rather than an error:
a checkout-less dev box must still be able to file requests. It becomes an
error at the one place it has consequences -- the claim, which refuses a row
with no `code_ref` (phase 3 order §5).

The committed-code check is the design's "only committed code may spend". Where
there is a checkout it is a real check (`git cat-file -e <sha>^{commit}`). Where
there is not, the sha came from the build arg and is by construction the commit
the image was built from, so the check degrades to `None` -- *cannot tell* --
and the caller records that rather than pretending either way.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

ENV_VAR = "DATA_DOWNLOAD_CODE_REF"

# The repo root, from `src/data_download/coderef.py`: two levels up. A path that
# does not exist means "no checkout", never an exception.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_git(args: list[str], repo_root: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git(args: list[str], repo_root: Path) -> str | None:
    """The command's output, or `None` if it did not succeed.

    Only for commands whose *answer* is their stdout (`rev-parse`). A command
    that answers by exiting -- `cat-file -e` prints nothing on success -- must
    use `_git_succeeds`, or an empty stdout reads as a failure. That bug would
    have refused every claim in a clean checkout.
    """
    finished = _run_git(args, repo_root)
    if finished is None or finished.returncode != 0:
        return None
    return finished.stdout.strip() or None


def _git_succeeds(args: list[str], repo_root: Path) -> bool:
    finished = _run_git(args, repo_root)
    return finished is not None and finished.returncode == 0


def resolve(env: dict[str, str] | None = None, repo_root: Path | None = None) -> str | None:
    """The commit this process runs, or `None` if nothing can say (§5).

    The environment wins over the checkout deliberately: in an image there is
    no checkout, and in a dev container that has one the environment is only set
    when someone meant it.
    """
    environ = os.environ if env is None else env
    from_env = (environ.get(ENV_VAR) or "").strip()
    if from_env:
        return from_env
    return _git(["rev-parse", "HEAD"], repo_root or REPO_ROOT)


def has_checkout(repo_root: Path | None = None) -> bool:
    root = repo_root or REPO_ROOT
    return (root / ".git").exists()


def is_commit(sha: str, repo_root: Path | None = None) -> bool | None:
    """Is `sha` a commit in this checkout? `None` means *cannot tell* (§5).

    `None` is not a failure and must not be read as one. It is the image case,
    where the sha is the build arg: the question "is it committed" was answered
    at build time, and re-asking it here without a checkout would be a guess
    dressed as a check.
    """
    root = repo_root or REPO_ROOT
    if not sha:
        return None
    if not has_checkout(root):
        return None
    return _git_succeeds(["cat-file", "-e", f"{sha}^{{commit}}"], root)


def resolve_and_log(**kwargs: object) -> str | None:
    value = resolve(**kwargs)  # type: ignore[arg-type]
    if value is None:
        log.info("no code_ref: neither %s nor a git checkout is available", ENV_VAR)
    else:
        log.info("code_ref %s", value)
    return value
