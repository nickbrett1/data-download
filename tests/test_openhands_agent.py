"""The OpenHands conversation client: four calls and the two traps in them.

The traps are the reason this file exists rather than being covered by a live
test. `idle` is what a conversation is created as, even when it starts running —
so "wait until it is not running" returns in the first second having done
nothing, and a build that never happened reads as a success. And attribution
lives in `tags`, because `observability_metadata` is accepted and dropped.
"""

from __future__ import annotations

import pytest

from data_download import openhands_agent


def test_idle_is_not_an_ending(monkeypatch):
    """A conversation is created `idle` *while it runs*: stopping there is the
    bug that makes every build look instantaneous and empty."""
    statuses = iter(["idle", "running", "idle", "finished"])
    monkeypatch.setattr(
        openhands_agent, "_call", lambda *a, **k: {"execution_status": next(statuses)}
    )
    monkeypatch.setattr(openhands_agent.time, "sleep", lambda _s: None)
    info = openhands_agent.wait("c", url="u", key="k", timeout=60, poll=0)
    assert info["execution_status"] == "finished"


def test_a_conversation_that_errors_is_returned_not_swallowed(monkeypatch):
    monkeypatch.setattr(
        openhands_agent, "_call", lambda *a, **k: {"execution_status": "error"}
    )
    info = openhands_agent.wait("c", url="u", key="k", timeout=60, poll=0)
    assert info["execution_status"] == "error"


def test_waiting_past_the_ceiling_is_a_refusal_that_names_the_state(monkeypatch):
    monkeypatch.setattr(
        openhands_agent, "_call", lambda *a, **k: {"execution_status": "running"}
    )
    monkeypatch.setattr(openhands_agent.time, "sleep", lambda _s: None)
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(openhands_agent.time, "monotonic", lambda: next(clock))
    with pytest.raises(openhands_agent.AgentError) as caught:
        openhands_agent.wait("c", url="u", key="k", timeout=10, poll=0)
    assert "still running" in str(caught.value)


def test_the_spend_line_comes_out_of_the_per_llm_stats():
    # Verbatim from GET /api/conversations/{id} after a real run: usage is
    # keyed by label, money is `accumulated_cost`, counts are nested. The first
    # version of this test guessed `cost`/`tokens` and so passed while the real
    # line printed "$0.0000, 0 tokens".
    info = {
        "stats": {
            "usage_to_metrics": {
                "default": {
                    "model_name": "openai/deepseek-v4-flash",
                    "accumulated_cost": 0.04319775600000001,
                    "max_budget_per_task": None,
                    "accumulated_token_usage": {
                        "model": "openai/deepseek-v4-flash",
                        "prompt_tokens": 3_642_482,
                        "completion_tokens": 38_170,
                        "cache_read_tokens": 3_578_752,
                        "cache_write_tokens": 0,
                        "reasoning_tokens": 16_176,
                        "context_window": 1_000_000,
                        "per_turn_token": 99_306,
                        "response_id": "",
                    },
                    "costs": [],
                    "response_latencies": [],
                    "token_usages": [],
                },
                "condenser": {
                    "model_name": "openai/deepseek-v4-flash",
                    "accumulated_cost": 0.0,
                    "accumulated_token_usage": {
                        "model": "openai/deepseek-v4-flash",
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "context_window": 0,
                        "response_id": "",
                    },
                },
            }
        }
    }
    line = openhands_agent.summarise(info)
    assert "$0.0432" in line
    # Prompt plus completion: the cache and reasoning counts are subsets of
    # those two, so adding every *_tokens key would double-count. Nor is the
    # context window spend.
    assert "3680652 tokens" in line


def test_no_stats_is_said_rather_than_shown_as_zero():
    assert openhands_agent.summarise({}) == "no cost recorded"
    # A server that reports the conversation without any usage is not a free
    # conversation; it is a missing number, and saying "$0.0000" would lie.
    assert openhands_agent.summarise({"stats": {"usage_to_metrics": {"m": {"accumulated_cost": 0.0}}}}) == "no cost recorded"


def test_the_transcript_lives_under_the_canvas_base_path():
    assert (
        openhands_agent.conversation_url("http://ryzen:3088/", "abc")
        == "http://ryzen:3088/canvas/conversations/abc"
    )


def test_tags_are_the_attribution_because_metadata_is_dropped():
    tags = openhands_agent._tags(
        ["caller=driver-loop", "vendor=Databento", "Bad Key=x", "noise", "branch=b-1"]
    )
    # Lowercase alphanumeric keys, unknown keys dropped, values kept verbatim.
    assert tags == {"caller": "driver-loop", "vendor": "Databento", "branch": "b-1"}


def test_the_key_is_read_from_the_container_that_generated_it(monkeypatch):
    class Done:
        returncode = 0
        stdout = "k" * 64 + "\n"
        stderr = ""

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(openhands_agent.subprocess, "run", fake_run)
    assert openhands_agent.session_key(container="openhands") == "k" * 64
    assert seen["argv"][:3] == ["docker", "exec", "openhands"]
    # The container path, not a host path: the key never leaves the host.
    assert openhands_agent.KEY_PATH in seen["argv"][-1]


def test_an_unreadable_key_is_misconfigured_not_a_broken_job(monkeypatch):
    class Done:
        returncode = 1
        stdout = ""
        stderr = "No such file or directory"

    monkeypatch.setattr(openhands_agent.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(openhands_agent.AgentConfig) as caught:
        openhands_agent.session_key(container="openhands")
    assert "OPENHANDS_API_KEY" in str(caught.value)


def test_start_asks_for_a_run_in_the_same_call(monkeypatch):
    captured = {}

    def fake_call(method, path, *, url, key, body=None, timeout=60.0):
        captured.update({"method": method, "path": path, "body": body})
        return {"id": "conv-1", "execution_status": "idle"}

    monkeypatch.setattr(openhands_agent, "_call", fake_call)
    info = openhands_agent.start(
        "do the thing",
        url="u",
        key="k",
        workdir="/projects/job-1",
        profile="p-1",
        tags={"vendor": "Databento"},
        budget=2.5,
        iterations=120,
    )
    assert info["id"] == "conv-1"
    body = captured["body"]
    assert captured["path"] == "/api/conversations"
    # Without this the conversation sits at `idle` with a message nobody acts on.
    assert body["initial_message_run"] is True
    assert body["workspace"] == {"working_dir": "/projects/job-1"}
    assert body["agent_profile_id"] == "p-1"
    assert body["tags"] == {"vendor": "Databento"}
    assert body["max_budget_per_run"] == 2.5
    assert body["max_iterations"] == 120


def test_a_full_server_is_a_wait_not_a_broken_job(monkeypatch):
    import io
    import urllib.error

    def full(*a, **k):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"too many"))

    monkeypatch.setattr(openhands_agent.urllib.request, "urlopen", full)
    with pytest.raises(openhands_agent.AgentError) as caught:
        openhands_agent._call("GET", "/health", url="http://x", key="k")
    assert not isinstance(caught.value, openhands_agent.AgentConfig)
    assert "capacity" in str(caught.value)
