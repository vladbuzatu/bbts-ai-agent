"""The 12 control-plane scenarios from the external review, plus a few more."""
from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

import orchestrator as orch_mod
from conftest import fake_claude, green_verify, rate_limited, red_verify, result, run


# 1. Session recovery after a container restart
def test_lost_session_starts_fresh_and_catches_up(make_orch, monkeypatch):
    o = make_orch()
    o.state.session_id = "gone-after-rebuild"
    fc = fake_claude(monkeypatch, Exception("Claude Code returned an error result: No conversation found with session ID: gone"),
                     "Caught up: we're at T-05.")
    reply = run(o.chat("where are we?"))
    assert reply == "Caught up: we're at T-05."
    assert fc.calls[0][1].resume == "gone-after-rebuild" and fc.calls[1][1].resume is None
    assert "history was lost" in fc.calls[1][0]
    assert o.state.session_id == "sess-1"


# 2. Recovery from corrupted state
def test_corrupted_state_file_does_not_crash(make_orch, base):
    (base / "data").mkdir(exist_ok=True)
    (base / "data" / "state.json").write_text("{not json")
    o = make_orch()
    assert o.state.session_id is None and o.status()


# 3. Claude errors and usage limits
def test_usage_limit_waits_then_continues(make_orch, monkeypatch):
    o = make_orch()
    real_sleep = asyncio.sleep
    slept = []

    async def fast_sleep(s):
        slept.append(s)
        await real_sleep(0)
    monkeypatch.setattr(orch_mod.asyncio, "sleep", fast_sleep)
    fake_claude(monkeypatch, rate_limited(), "continued fine")
    assert run(o.chat("go")) == "continued fine"
    assert any(s >= 60 for s in slept), "should wait for the limit to reset"
    assert "usage limit" in o.rec.last("usage limit")
    assert o.state.usage["five_hour"]["utilization"] == 1.0


def test_agent_error_pauses_the_build(make_orch, monkeypatch):
    o = make_orch()
    green_verify(o)
    fake_claude(monkeypatch, Exception("CLI crashed"))

    async def go():
        o.start_build()
        await o.build_task
    run(go())
    assert "Build paused" in o.rec.last("paused")


# 4. Build cancellation
def test_stop_cancels_a_running_build(make_orch, monkeypatch):
    o = make_orch()
    started = asyncio.Event()

    def working_forever(prompt, options):
        async def gen():
            started.set()
            await asyncio.sleep(3600)
            yield result("never")
        return gen()
    monkeypatch.setattr(orch_mod, "query", working_forever)

    async def go():
        o.start_build()
        await started.wait()
        assert o.ops.busy() and "build round 1" in o.ops.busy()
        assert o.stop()
        with pytest.raises(asyncio.CancelledError):
            await o.build_task
    run(go())
    assert o.rec.last("Build stopped")
    assert not o.lock.locked(), "the controller must be released after a stop"


# 5. Telegram delivery failures never crash the bot
def test_send_survives_telegram_errors(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    import bot
    from telegram.error import BadRequest

    class FailingBot:
        async def send_message(self, **kw):
            raise BadRequest("Chat not found")

    class App:
        bot = FailingBot()
    assert run(bot.send(App(), "hello", buttons=[[("A", "a")]])) is None


# 6. Duplicate /build requests
def test_duplicate_build_is_refused(make_orch, monkeypatch):
    o = make_orch()
    green_verify(o)
    fake_claude(monkeypatch, "round done")

    async def go():
        assert o.start_build() is True
        assert o.start_build() is False
        await o.build_task
    run(go())


# 7. Concurrent operations: the controller refuses instead of racing
def test_operations_do_not_collide(make_orch, monkeypatch):
    o = make_orch()

    async def go():
        async with o.ops.run("build round 3"):
            msg = o.ops.busy()
            assert msg and "build round 3" in msg
            assert "Busy" in (await o.studio.start("commercial-offer", "Client X"))
            assert o.curator.approve() in ("No proposal waiting.",) or "Busy" in o.curator.approve()
        assert o.ops.busy() is None
    run(go())


# 8. Git push failures are reported, not hidden
def test_push_success_then_rejection(make_orch, base):
    o = make_orch(github=True)
    assert "Pushed to GitHub" in run(o.push())
    assert "already up to date" in run(o.push())
    # someone edits GitHub directly -> the remote has a commit the agents don't have
    other = base / "other"
    subprocess.run(["git", "clone", "-q", str(base / "remote.git"), str(other)], check=True)
    (other / "x.txt").write_text("edited on GitHub")
    subprocess.run(["git", "-C", str(other), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qam", "web edit"], check=False)
    subprocess.run(["git", "-C", str(other), "add", "x.txt"], check=True)
    subprocess.run(["git", "-C", str(other), "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "web edit"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "-q", "origin", "main"], check=True)
    (o.cfg.workspace / "y.txt").write_text("agent work")
    import sandbox
    sandbox.git("add", "y.txt", cwd=o.cfg.workspace)
    sandbox.git("commit", "-qm", "T-02: y", cwd=o.cfg.workspace)
    msg = run(o.push())
    assert "Push to GitHub failed" in msg and "edited on GitHub" in msg


# 9. GitHub Actions failures reach the owner AND the agents (who have no GitHub access)
def test_pipeline_failure_hands_the_log_to_the_devops_agent(make_orch, monkeypatch):
    o = make_orch(github=True)
    monkeypatch.setenv("FAKE_GH_RESULT", "failure")
    run(o._watch_pipeline("abc1234def"))
    log = o.cfg.workspace / ".ci-logs" / "run-77.log"
    assert log.exists() and "whitenoise" in log.read_text()
    assert ".ci-logs/" in (o.cfg.workspace / ".gitignore").read_text()
    assert any("pipeline FAILED" in m and ".ci-logs/run-77.log" in m for m in o.state.inbox)
    assert "pipeline failed" in o.rec.last("pipeline failed")


def test_pipeline_success_offers_deploy_button(make_orch, monkeypatch):
    o = make_orch(github=True)
    monkeypatch.setenv("FAKE_GH_RESULT", "success")
    run(o._watch_pipeline("abc1234def"))
    assert "pipeline passed" in o.rec.msgs[-1]
    assert ("🚀 Deploy to production…", "deploy:preview") in o.rec.buttons[-1][0]


# 10. Production deploys only happen through the bot
def test_production_deploy_is_triggered_only_by_the_bot(make_orch, monkeypatch, base):
    o = make_orch(github=True)
    monkeypatch.setenv("FAKE_GH_RESULT", "success")
    msg = run(o.deploy_production())
    assert "Production deploy started" in msg
    assert "workflow run deploy-production.yml" in (base / "gh.log").read_text()
    # agents get no GitHub token: blanked in their env and absent from the bot's environment
    import os
    import sandbox
    assert "GITHUB_TOKEN" not in os.environ and sandbox.agent_env()["GITHUB_TOKEN"] == ""
    assert o._options().env["GITHUB_TOKEN"] == ""


def test_deploy_preview_without_staging_version_offers_no_button(make_orch):
    o = make_orch(github=True)
    text, can = run(o.deploy_preview())
    assert can is False and "nothing to promote" in text


# 12. Owner input and build resumption
def test_question_pauses_build_and_answer_resumes_it(make_orch, monkeypatch):
    o = make_orch()
    green_verify(o)
    fake_claude(monkeypatch, "Did T-03.\nNEEDS_INPUT: Light or dark theme? (default: light)",
                "Light it is.", "Built.\nPROJECT_COMPLETE: done")

    async def go():
        o.start_build()
        await o.build_task
        assert o.state.awaiting_input
        assert o.rec.buttons[-1] == [[("👍 Use your suggestions", "pm:defaults")]]
        reply = await o.chat("light")
        assert "Resuming the build" in reply
        await o.build_task
    run(go())
    assert "complete and verified" in o.rec.last("complete")


def test_failing_tests_are_fed_back_and_false_completion_rejected(make_orch, monkeypatch):
    o = make_orch(max_rounds=2)
    red_verify(o)
    fc = fake_claude(monkeypatch, "work", "PROJECT_COMPLETE: done")

    async def go():
        o.start_build()
        await o.build_task
    run(go())
    assert "FAILED" in fc.calls[1][0]
    assert not o.rec.last("complete and verified")
