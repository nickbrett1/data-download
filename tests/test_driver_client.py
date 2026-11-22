"""Phase B: the driver across the wire, and the proof that nothing changed.

The claim this file has to earn is narrow and unforgiving: **`RemoteDriver` is a
transport, not a behaviour change.** So the centre of the file is not "the eight
verbs return the right shapes" (that is phase A's, in the other repo) but:

    interpreter.run(job, RemoteDriver(...)) == interpreter.run(job, StubDatabentoDriver(...))

asserted field by field, for every mode and both `on_failure` policies. If a
single field differs -- a verdict, a disposition, a row count, the step list --
the wire is doing something to the answer, and that is the bug this phase exists
to catch before any real driver is behind it.

The rest are the rules that make the transport safe rather than merely working:
a missing credential never sends, a `dest` that climbs never travels, a refusal
keeps the runtime's own code, and a broken verifier reaches `classify()` intact
so it can be called `verifier_error` instead of being smoothed into a plausible
shortfall.
"""

from __future__ import annotations

import pytest

from conftest import PLAN_BODY
from data_download import interpreter
from data_download.driver_client import (
    PROTOCOL_VERSION,
    DriverUnavailable,
    RemoteDriver,
)
from data_download.profiles import databento_batch_download as databento
from data_download.stub import StubDatabentoDriver
from fake_runtime import FakeRuntime


def _job(on_failure: str = "quarantine"):
    return databento.bind(PLAN_BODY["params"], {"on_failure": on_failure})


def _root(datasets_dir, tag: str):
    root = datasets_dir / "databento" / "GLBX.MDP3" / f"req_{tag}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _driver(runtime, root, mode="clean"):
    return RemoteDriver(root, mode, url=runtime.url, token=runtime.token)


# -- the exit test -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["clean", "short", "nothing"])
@pytest.mark.parametrize("on_failure", ["quarantine", "commit_marked"])
def test_the_wire_produces_the_same_verdict_as_the_in_process_driver(
    tmp_path, mode, on_failure
):
    """The phase's whole exit test: identical Verdict, field by field."""
    remote_root = _root(tmp_path / "remote", "parity")
    local_root = _root(tmp_path / "local", "parity")

    with FakeRuntime(tmp_path / "remote") as runtime:
        remote = _driver(runtime, remote_root, mode)
        try:
            over_the_wire = interpreter.run(_job(on_failure), remote)
        finally:
            remote.close()

    in_process = interpreter.run(_job(on_failure), StubDatabentoDriver(local_root, mode))

    assert over_the_wire.as_dict() == in_process.as_dict()

    # And the bytes agree too, not just the verdict that summarised them: the
    # same names landed, and each says what it is.
    #
    # `_landed()` reads a missing `files/` as empty rather than failing, because
    # that is how the verifier reads it and the two implementations differ
    # harmlessly here: the artefact (and `fake_runtime`) create the staging
    # directory even when the vendor published nothing, while the app's
    # in-process stub leaves it uncreated in `nothing` mode. The directory's
    # existence is not a file fact; `staged: {}` is the claim, and both sides
    # make it. The artefact's behaviour is the one that survives phase C.
    landed_remote = _landed(remote_root)
    landed_local = _landed(local_root)
    assert landed_remote == landed_local
    for name in landed_remote:
        assert (remote_root / "files" / name).read_bytes().startswith(b"STUB: not market data.")
        assert (local_root / "files" / name).read_bytes().startswith(b"STUB: not market data.")


def _landed(root) -> list[str]:
    staging = root / "files"
    if not staging.is_dir():
        return []
    return sorted(p.name for p in staging.iterdir() if p.is_file())


def test_the_wire_is_the_one_thing_that_changed(tmp_path):
    """Named explicitly, because the parity test would pass even if both were
    wrong in the same way: clean mode is a pass that commits."""
    remote_root = _root(tmp_path / "remote", "clean")
    with FakeRuntime(tmp_path / "remote") as runtime:
        verdict = interpreter.run(_job(), _driver(runtime, remote_root))

    assert verdict.verdict == interpreter.PASS
    assert verdict.disposition == interpreter.COMMIT
    assert verdict.steps == (
        "resolve", "quote", "submit", "poll", "download", "convert", "verify",
    )
    assert verdict.detail["files"] == 2  # 2026-10-01 .. 2026-10-03, one per day
    assert verdict.detail["rows"] == 2000
    assert verdict.detail["quote_usd"] == pytest.approx(12.34)
    assert verdict.interpreter_version == interpreter.INTERPRETER_VERSION


# -- the eight verbs, one at a time -----------------------------------------


def test_every_verb_answers_in_the_shape_the_interpreter_asks_for(tmp_path):
    """The protocol's types, asserted on the proxy rather than on the verdict:
    `cost` is a float here even though the wire carries `{"usd": ...}`."""
    with FakeRuntime(tmp_path) as runtime:
        driver = _driver(runtime, _root(tmp_path, "verbs"))
        with driver:
            assert driver.resolve(symbols=["ES.c.0"], stype_in="continuous")["resolved"] == 1
            assert isinstance(driver.cost(), float)
            submitted = driver.submit(
                dataset="GLBX.MDP3",
                start="2026-10-01",
                end="2026-10-03",
                encoding="dbn",
                compression="zstd",
            )
            assert submitted["vendor_job_id"].startswith("stub_")
            assert driver.poll(vendor_job_id=submitted["vendor_job_id"])["state"] == "done"
            files = driver.list_files(vendor_job_id=submitted["vendor_job_id"])
            assert [f["name"] for f in files] == [
                "GLBX.MDP3-2026-10-01.dbn.zst",
                "GLBX.MDP3-2026-10-02.dbn.zst",
            ]
            written = driver.fetch(
                vendor_job_id=submitted["vendor_job_id"], dest="files/", files=files
            )
            assert sorted(written) == sorted(f["name"] for f in files)
            assert sorted(driver.convert(dest="files/")) == sorted(written)
            evidence = driver.verify(
                dest="files/",
                claimed=[f["name"] for f in files],
                staged={"ignored": "the runtime re-reads the disk, deliberately"},
            )
            assert evidence["claimed"] == [f["name"] for f in files]
            assert set(evidence["staged"]) == set(written)
        assert driver.calls == [
            "resolve", "cost", "submit", "poll", "list_files", "fetch", "convert", "verify",
        ]


def test_one_session_serves_the_whole_job_and_closing_kills_it(tmp_path):
    with FakeRuntime(tmp_path) as runtime:
        driver = _driver(runtime, _root(tmp_path, "session"))
        driver.open()
        session_id = driver._session_id
        driver.open()  # idempotent: one Job, one process, not two
        assert driver._session_id == session_id
        assert list(runtime.sessions) == [session_id]

        driver.close()
        assert runtime.sessions == {}
        assert driver._session_id is None
        driver.close()  # closing twice is not an error

    assert runtime.requests.count(("POST", "/v1/sessions")) == 1


# -- the rules that make it safe --------------------------------------------


def test_no_token_means_no_connection_is_attempted(tmp_path):
    """A client that can silently send no credential is the client that will."""
    with FakeRuntime(tmp_path) as runtime:
        driver = RemoteDriver(_root(tmp_path, "notoken"), url=runtime.url, token="")
        before = list(runtime.requests)
        with pytest.raises(DriverUnavailable, match="DRIVER_RUNTIME_TOKEN"):
            driver.cost()
        assert runtime.requests == before, "the request must not have been sent"


def test_a_wrong_token_is_refused_by_the_runtime_not_the_client(tmp_path):
    """The other half: the credential is checked on the far side too."""
    with FakeRuntime(tmp_path) as runtime:
        driver = RemoteDriver(
            _root(tmp_path, "badtoken"), url=runtime.url, token="not-the-minted-one"
        )
        with pytest.raises(DriverUnavailable, match="401"):
            driver.open()


def test_a_dest_that_climbs_is_refused_before_it_travels(tmp_path):
    with FakeRuntime(tmp_path) as runtime:
        driver = _driver(runtime, _root(tmp_path, "dest"))
        with driver:
            before = len(runtime.requests)
            for bad in ("../outside", "/etc", "files/../../escape"):
                with pytest.raises(interpreter.InterpreterError, match="relative"):
                    driver.fetch(vendor_job_id="v", dest=bad, files=[])
            assert len(runtime.requests) == before


def test_a_broken_verifier_stays_loud_instead_of_becoming_a_shortfall(tmp_path):
    """The one failure the protocol must not smooth over.

    A runtime that answers `{"manifest": "nonsense"}` is a broken verifier, not a
    vendor that published less than it claimed. The proxy passes it through and
    `classify` names it, so the row quarantines with that reason rather than
    committing a landing nobody checked.
    """
    with FakeRuntime(tmp_path) as runtime:
        runtime.overrides["verify"] = {"manifest": "nonsense"}
        verdict = interpreter.run(_job(), _driver(runtime, _root(tmp_path, "broken")))

    assert verdict.verdict == interpreter.VERIFIER_ERROR
    assert verdict.disposition == interpreter.QUARANTINE
    assert verdict.detail["reason"] == "unparseable manifest"


def test_a_missing_driver_says_which_driver_and_what_to_do(tmp_path):
    with FakeRuntime(tmp_path) as runtime:
        driver = RemoteDriver(
            _root(tmp_path, "missing"), url=runtime.url, token=runtime.token, driver="databento"
        )
        with pytest.raises(DriverUnavailable) as caught:
            driver.open()
    message = str(caught.value)
    assert "driver_missing" in message
    assert "databento" in message and "installed" in message


def test_an_unreachable_runtime_is_an_interpreter_error_not_a_crash(tmp_path):
    """Closed port: the runner must settle `failed`, not raise out of `run_one`."""
    driver = RemoteDriver(
        _root(tmp_path, "closed"), url="http://127.0.0.1:1", token="minted"
    )
    with pytest.raises(DriverUnavailable):
        driver.open()
    assert issubclass(DriverUnavailable, interpreter.InterpreterError)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://data-download-drivers:8831", "must be http"),
        ("http://", "names no host"),
    ],
)
def test_a_url_that_is_not_a_loopback_or_service_name_is_refused(tmp_path, url, reason):
    with pytest.raises(DriverUnavailable, match=reason):
        RemoteDriver(_root(tmp_path, "url"), url=url, token="minted")


def test_a_public_hostname_is_refused_by_the_transport(tmp_path):
    """The containment rule in `runtime_http`: a dotted name is an egress path."""
    driver = RemoteDriver(
        _root(tmp_path, "fqdn"), url="http://example.com:8831", token="minted"
    )
    with pytest.raises(DriverUnavailable, match="egress"):
        driver.open()


def test_the_protocol_version_is_the_wire_s_not_the_interpreter_s(tmp_path):
    """Two integers-that-are-not-one-integer: which transport carried the verbs,
    and which flow ran. They happen to both be 1 today; the test asserts they are
    different *kinds* of fact, not different numbers."""
    with FakeRuntime(tmp_path) as runtime:
        driver = _driver(runtime, _root(tmp_path, "protocol"))
        with driver:
            assert PROTOCOL_VERSION == 1
            assert driver.session_driver_version == "1"
    assert isinstance(PROTOCOL_VERSION, int)
    assert isinstance(interpreter.INTERPRETER_VERSION, str)


def test_a_runtime_speaking_another_protocol_is_a_clear_refusal(tmp_path):
    with FakeRuntime(tmp_path) as runtime:
        driver = RemoteDriver(
            _root(tmp_path, "v2"), url=runtime.url, token=runtime.token, protocol=2
        )
        with pytest.raises(DriverUnavailable, match="unsupported protocol"):
            driver.open()
