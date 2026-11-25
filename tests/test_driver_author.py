"""The authoring harness: a ticket in, a branch and a sha out.

The contract the loop depends on is two lines of text and an exit code, so most
of this is about the *refusals*: an agent that pushed nothing, a branch the
forge has never heard of, and a token that is missing all have to stop the job
with a sentence rather than hand the loop a sha it cannot gate.
"""

from __future__ import annotations

import io
import json
import os
import stat
import time
from datetime import UTC

import pytest

from data_download import driver_author, github_api

TICKET = (
    "Write a driver for the vendor 'Databento'.\n"
    "\n"
    "Plan types waiting on it: market_data_window\n"
    "The first request waiting is for dataset GLBX.MDP3.\n"
)


def test_the_vendor_comes_out_of_the_tickets_first_line():
    assert driver_author.vendor_of(TICKET) == "Databento"
    # A ticket whose first line changes shape must still produce a branch.
    assert driver_author.vendor_of("Something else entirely") == "unknown"
    assert driver_author.vendor_of("") == "unknown"


def test_the_branch_is_named_by_the_harness_and_never_reused():
    """The loop gates a sha, so a branch that already exists could answer with a
    commit it has already judged. Timestamped names make reuse impossible."""
    from datetime import datetime

    when = datetime(2026, 11, 25, 4, 12, 33, tzinfo=UTC)
    assert (
        driver_author.branch_for("Databento", now=when)
        == "driver-loop/databento-20261125t041233z"
    )
    assert driver_author.branch_for("Nabu Inc.", now=when) == "driver-loop/nabu-inc-20261125t041233z"
    assert driver_author.branch_for("???", now=when) == "driver-loop/unknown-20261125t041233z"


def test_the_prompt_names_the_branch_the_push_and_what_may_change():
    prompt = driver_author.prompt_for(
        TICKET,
        repo="nickbrett1/data-download-drivers",
        branch="driver-loop/databento-20261125t041233z",
        base="main",
        workdir="/projects/driver-loop/driver-loop-databento-20261125t041233z",
        credential="/projects/driver-loop/job/push-credential",
    )
    assert "Write a driver for the vendor 'Databento'" in prompt
    assert "driver-loop/databento-20261125t041233z" in prompt
    # The push is the deliverable: nothing is collected from the workspace.
    assert "git push" in prompt
    assert "HEAD:refs/heads/driver-loop/databento-20261125t041233z" in prompt
    assert "/projects/driver-loop/job/push-credential" in prompt
    # The harness owns the PR and the merge, so the agent is told neither to
    # push to the base branch nor to open a pull request.
    assert "do not open a pull request" in prompt.lower()
    assert "Do not push to main" in prompt
    assert "drivers/ or tests/fixtures/" in prompt


def test_a_prompt_with_no_credential_does_not_pretend_there_is_one():
    prompt = driver_author.prompt_for(
        TICKET, repo="o/r", branch="b", base="main", workdir="/w", credential=None
    )
    assert "push-credential" not in prompt
    assert "git push origin HEAD:refs/heads/b" in prompt


def test_a_missing_branch_is_a_refusal_not_an_empty_sha(monkeypatch):
    def no_branch(repo, ref, *, token):
        raise github_api.GithubError(f"the forge has no branch {ref!r} on {repo}: 404")

    monkeypatch.setattr(driver_author.github_api, "head_sha", no_branch)
    with pytest.raises(driver_author.Refused) as caught:
        driver_author.verify("o/r", "driver-loop/x-1", "main", token="t")
    assert "nothing was pushed" in str(caught.value)


def test_a_branch_still_at_the_base_is_the_agent_having_committed_nothing(monkeypatch):
    monkeypatch.setattr(driver_author.github_api, "head_sha", lambda repo, ref, *, token: "a" * 40)
    with pytest.raises(driver_author.Refused) as caught:
        driver_author.verify("o/r", "driver-loop/x-1", "main", token="t")
    assert "committed nothing" in str(caught.value)


def test_the_answer_is_the_forges_sha_for_the_branch(monkeypatch):
    shas = {"main": "a" * 40, "driver-loop/x-1": "b" * 40}
    monkeypatch.setattr(
        driver_author.github_api, "head_sha", lambda repo, ref, *, token: shas[ref]
    )
    answer = driver_author.verify("o/r", "driver-loop/x-1", "main", token="t")
    # A local `rev-parse` would be a statement about a checkout; this is a
    # statement about the branch the loop will gate.
    assert answer == {"commit_sha": "b" * 40, "base_sha": "a" * 40}


def test_no_token_is_misconfigured_rather_than_a_refusal():
    with pytest.raises(driver_author.Misconfigured):
        driver_author.verify("o/r", "b", "main", token="")


def test_the_transcript_id_is_carried_up_only_when_there_is_one():
    assert driver_author.author_ref_of("noise\nauthor-ref: conv-123\nmore\n") == "conv-123"
    # A plain shell agent has no transcript, and that is not a failure.
    assert driver_author.author_ref_of("some output\n") == ""


def _agent(tmp_path, body: str):
    script = tmp_path / "agent.sh"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _run_main(monkeypatch, argv, ticket=TICKET):
    monkeypatch.setattr("sys.stdin", io.StringIO(ticket))
    return driver_author.main(argv)


def test_a_ticket_becomes_one_json_line_on_stdout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(driver_author, "write_credential", lambda *a, **k: None)
    monkeypatch.setattr(driver_author, "remove_credential", lambda *a, **k: None)
    shas = {"main": "a" * 40}
    monkeypatch.setattr(
        driver_author.github_api,
        "head_sha",
        lambda repo, ref, *, token: shas.get(ref, "c" * 40),
    )
    monkeypatch.setattr(
        driver_author.github_api,
        "ensure_pull_request",
        lambda repo, *, head, base, title, body, token: {"number": 7, "created": True},
    )
    status = _run_main(
        monkeypatch,
        ["--repo", "o/r", "--base", "main", "--agent-cmd", _agent(tmp_path, "echo 'author-ref: conv-9'\n")],
    )
    assert status == 0
    line = capsys.readouterr().out.strip().splitlines()[-1]
    answer = json.loads(line)
    assert answer["commit_sha"] == "c" * 40
    assert answer["branch"].startswith("driver-loop/databento-")
    assert answer["author_ref"] == "conv-9"


def test_an_agent_that_fails_stops_the_job_with_its_own_words(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    status = _run_main(
        monkeypatch,
        [
            "--repo",
            "o/r",
            "--agent-cmd",
            _agent(tmp_path, "echo 'the model gave up' >&2\nexit 3\n"),
        ],
    )
    assert status == 1
    out = capsys.readouterr()
    assert "REFUSED" in out.err
    assert "the model gave up" in out.err


def test_an_empty_ticket_is_refused_before_anything_is_started(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    status = _run_main(monkeypatch, ["--repo", "o/r"], ticket="   ")
    assert status == 1
    assert "no ticket on stdin" in capsys.readouterr().err


def test_a_missing_agent_command_is_misconfigured(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    status = _run_main(monkeypatch, ["--repo", "o/r", "--agent-cmd", "  "])
    assert status == 2
    assert "MISCONFIGURED" in capsys.readouterr().err


def test_a_credential_left_by_a_killed_run_is_cleared_but_not_a_live_one(monkeypatch):
    # `finally` does not run on SIGKILL, so the file survives the process. The
    # guard is age: a file younger than the authoring timeout might belong to a
    # job still in flight, so the sweep must not touch it.
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return type("Done", (), {"returncode": 0, "stdout": "2\n", "stderr": ""})()

    monkeypatch.setattr(driver_author.subprocess, "run", fake_run)
    removed = driver_author.sweep_credentials("openhands", "/projects/driver-loop/", older_than=1800)

    assert removed == 2
    assert "-mmin" in " ".join(seen["argv"])
    assert seen["argv"][-1] == "30"  # 1800s, in minutes, never 0


def test_a_sweep_that_cannot_speak_docker_is_not_fatal(monkeypatch):
    def no_docker(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(driver_author.subprocess, "run", no_docker)
    assert driver_author.sweep_credentials("openhands", "/projects", older_than=60) == 0


def test_an_agent_that_pushed_and_then_failed_is_still_an_answer(tmp_path, monkeypatch, capsys):
    # Seen in the wild: the model emitted `risk: 'LOW'` on a terminal call, the
    # tool rejected it, and the run ended on a retryable tool error -- after the
    # push. The branch is the evidence; the agent's exit code is not.
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(driver_author, "sweep_credentials", lambda *a, **k: 0)
    monkeypatch.setattr(driver_author, "write_credential", lambda *a, **k: None)
    monkeypatch.setattr(driver_author, "remove_credential", lambda *a, **k: None)
    shas = {"main": "a" * 40}
    monkeypatch.setattr(
        driver_author.github_api, "head_sha", lambda repo, ref, *, token: shas.get(ref, "c" * 40)
    )
    monkeypatch.setattr(
        driver_author.github_api,
        "ensure_pull_request",
        lambda repo, *, head, base, title, body, token: {"number": 8, "created": True},
    )
    agent = _agent(
        tmp_path,
        "echo 'author-ref: conv-9'\necho 'openhands: the conversation ended error' >&2\nexit 1\n",
    )
    status = _run_main(monkeypatch, ["--repo", "o/r", "--base", "main", "--agent-cmd", agent])

    assert status == 0
    answer = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert answer["commit_sha"] == "c" * 40
    assert answer["author_ref"] == "conv-9"


def test_when_the_agent_failed_and_nothing_was_pushed_the_agents_reason_is_kept(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(driver_author, "sweep_credentials", lambda *a, **k: 0)
    monkeypatch.setattr(driver_author, "write_credential", lambda *a, **k: None)
    monkeypatch.setattr(driver_author, "remove_credential", lambda *a, **k: None)
    monkeypatch.setattr(
        driver_author.github_api, "head_sha", lambda repo, ref, *, token: "a" * 40
    )
    agent = _agent(tmp_path, "echo 'openhands: REFUSED: budget exhausted' >&2\nexit 1\n")
    status = _run_main(monkeypatch, ["--repo", "o/r", "--base", "main", "--agent-cmd", agent])

    assert status == 1
    assert "budget exhausted" in capsys.readouterr().err


# -- where the credential goes ------------------------------------------------


def test_a_shared_workspace_writes_the_credential_at_the_path_the_agent_sees(tmp_path):
    """The control plane mounts the agent's /projects, so the path is not
    translated: what the prompt says is what this process writes."""
    workspace = driver_author.SharedWorkspace(tmp_path)
    target = tmp_path / "driver-loop" / "driver-loop-x" / "push-credential"
    workspace.write(str(target), "tok")

    assert target.read_text() == "tok"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    workspace.remove(str(target))
    assert not target.exists()


def test_the_credential_is_handed_to_the_agent_uid(tmp_path, monkeypatch):
    """Root writes it, uid 10001 has to read it.

    The app image declares no USER, so the file lands root-owned; the agent is
    uid 10001 in another container and would get EACCES. The failure would not
    be an error anywhere -- just an agent with no credential -- so the handover
    is asserted, not assumed.
    """
    given: list[tuple[str, int, int]] = []
    monkeypatch.setattr(driver_author.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        driver_author.os, "chown", lambda p, u, g: given.append((os.path.basename(p), u, g))
    )

    workspace = driver_author.SharedWorkspace(tmp_path, owner=(10001, 10001))
    target = tmp_path / "driver-loop" / "run-1" / "push-credential"
    workspace.write(str(target), "tok")

    assert target.read_text() == "tok"
    assert ("push-credential", 10001, 10001) in given
    # The directories on the way must be traversable by that uid, which 0700
    # (the mkdtemp default) is not.
    assert stat.S_IMODE((tmp_path / "driver-loop").stat().st_mode) == 0o755
    assert ("run-1", 10001, 10001) in given


def test_nothing_is_chowned_when_the_agent_is_the_same_user(tmp_path, monkeypatch):
    """A host run is already the right uid, and non-root cannot chown anyway."""
    monkeypatch.setattr(driver_author.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        driver_author.os, "chown", lambda *a: pytest.fail("chown must not be attempted")
    )
    target = tmp_path / "driver-loop" / "run-1" / "push-credential"
    driver_author.SharedWorkspace(tmp_path, owner=(10001, 10001)).write(str(target), "tok")
    assert target.read_text() == "tok"


def test_a_shared_workspace_refuses_a_credential_outside_its_mount(tmp_path):
    workspace = driver_author.SharedWorkspace(tmp_path / "projects")
    with pytest.raises(driver_author.Misconfigured, match="not under"):
        workspace.write("/projects/driver-loop/x/push-credential", "tok")


def test_a_shared_workspace_sweeps_only_stale_credentials(tmp_path):
    workspace = driver_author.SharedWorkspace(tmp_path)
    stale = tmp_path / "driver-loop" / "old" / "push-credential"
    live = tmp_path / "driver-loop" / "new" / "push-credential"
    for path in (stale, live):
        path.parent.mkdir(parents=True)
        path.write_text("tok")
    old = time.time() - 7200
    os.utime(stale, (old, old))

    assert workspace.sweep(str(tmp_path / "driver-loop"), older_than=1800) == 1
    assert not stale.exists() and live.exists()


def test_authoring_goes_through_a_conversation_when_there_is_no_command(tmp_path, monkeypatch):
    """The app authors in-process: no subprocess, so the transcript id is the
    agent's answer rather than a line printed on stdout."""
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen = {}

    def converse(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return {"conversation": "conv-77", "status": "finished", "info": {}, "said": ""}

    monkeypatch.setattr(driver_author.openhands_agent, "converse", converse)
    monkeypatch.setattr(
        driver_author.github_api,
        "head_sha",
        lambda repo, ref, *, token: "a" * 40 if ref == "main" else "b" * 40,
    )
    monkeypatch.setattr(
        driver_author.github_api,
        "ensure_pull_request",
        lambda repo, *, head, base, title, body, token: {"number": 9, "created": True},
    )
    answer = driver_author.author(
        "Write a driver for the vendor 'Zzz'.\n",
        repo="o/r",
        token="tok",
        projects_dir=str(tmp_path),
        workdir_prefix=str(tmp_path / "driver-loop"),
        say=lambda line: None,
    )

    assert answer == {"branch": answer["branch"], "commit_sha": "b" * 40, "author_ref": "conv-77"}
    assert answer["branch"].startswith("driver-loop/zzz-")
    assert seen["url"]  # the agent server, not an empty string
    # No key file named here, so `converse` keeps its own fallback -- an
    # operator's host can `docker exec` the agent server, an app container
    # cannot. A deployment names the file (the next test).
    assert seen["key_file"] == ""
    assert "Zzz" in seen["prompt"]
    # The credential was written for the run and taken away afterwards.
    assert list(tmp_path.glob("driver-loop/*/push-credential")) == []


def test_the_session_key_is_named_by_path_not_fetched_with_docker(tmp_path, monkeypatch):
    """The app container has no docker CLI and no socket, by design.

    Without a key file `converse` shells out to `docker exec openhands cat
    <key>`, which in the deployed container is `[Errno 2] No such file or
    directory: 'docker'` -- and that arrived as a `needs_human` driver job, not
    as a 401. So the path the app can read has to be passed all the way down.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    seen = {}

    def converse(prompt, **kwargs):
        seen.update(prompt=prompt, **kwargs)
        return {"conversation": "conv-88", "status": "finished", "info": {}, "said": ""}

    monkeypatch.setattr(driver_author.openhands_agent, "converse", converse)
    monkeypatch.setattr(
        driver_author.github_api,
        "head_sha",
        lambda repo, ref, *, token: "a" * 40 if ref == "main" else "b" * 40,
    )
    monkeypatch.setattr(
        driver_author.github_api,
        "ensure_pull_request",
        lambda repo, *, head, base, title, body, token: {"number": 9, "created": True},
    )
    driver_author.author(
        "Write a driver for the vendor 'Zzz'.\n",
        repo="o/r",
        token="tok",
        projects_dir=str(tmp_path),
        workdir_prefix=str(tmp_path / "driver-loop"),
        key_file="/run/openhands/api-key.txt",
        say=lambda line: None,
    )

    assert seen["key_file"] == "/run/openhands/api-key.txt"


def test_an_authoring_conversation_that_failed_still_names_the_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")

    def converse(prompt, **kwargs):
        raise driver_author.openhands_agent.AgentError("the conversation ended error")

    error = driver_author.openhands_agent.AgentError("the conversation ended error")
    error.conversation = "conv-99"
    monkeypatch.setattr(driver_author.openhands_agent, "converse", lambda *a, **k: (_ for _ in ()).throw(error))
    monkeypatch.setattr(
        driver_author.github_api,
        "head_sha",
        lambda repo, ref, *, token: "a" * 40 if ref == "main" else "b" * 40,
    )
    monkeypatch.setattr(
        driver_author.github_api,
        "ensure_pull_request",
        lambda repo, *, head, base, title, body, token: {"number": 9, "created": True},
    )
    answer = driver_author.author(
        "Write a driver for the vendor 'Zzz'.\n",
        repo="o/r",
        token="tok",
        projects_dir=str(tmp_path),
        workdir_prefix=str(tmp_path / "driver-loop"),
        say=lambda line: None,
    )
    # The forge had the branch, so the run is an answer -- and the id is kept.
    assert answer["author_ref"] == "conv-99"
