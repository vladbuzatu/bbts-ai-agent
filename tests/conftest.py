"""Shared fixtures for the bot's own test suite.

Run inside the container:   docker compose run --rm agents pytest -q /app/tests
Run on a laptop:            cd agent-team-kit && pytest -q tests
(Privilege-separation tests only run where they can: as root with an 'agent' user.)
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "bot"))
FAKES = KIT / "tests" / "fakes"

# Secrets must never leak into child processes: simulate the bot's startup.
os.environ.setdefault("GITHUB_TOKEN", "ghp_TEST_SECRET")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "1:TG_TEST_SECRET")
import sandbox  # noqa: E402

sandbox.load_and_scrub()

import orchestrator as orch_mod  # noqa: E402
from claude_agent_sdk import RateLimitEvent, ResultMessage, SystemMessage  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def base():
    """A temp dir the agent user can traverse (pytest's own tmp is root-only)."""
    d = Path(tempfile.mkdtemp(prefix="kit-test-"))
    os.chmod(d, 0o755)
    yield d
    subprocess.run(["rm", "-rf", str(d)], check=False)


class Recorder:
    def __init__(self):
        self.msgs: list[str] = []
        self.buttons: list = []

    async def __call__(self, text, buttons=None):
        self.msgs.append(text)
        self.buttons.append(buttons)

    def last(self, contains: str = "") -> str:
        for m in reversed(self.msgs):
            if contains in m:
                return m
        return ""


def result(text, *, error=False, session="sess-1"):
    return ResultMessage(subtype="success" if not error else "error_during_execution", duration_ms=1,
                         duration_api_ms=1, is_error=error, num_turns=1, session_id=session,
                         stop_reason=None, total_cost_usd=0, usage={}, result=text)


class FakeClaude:
    """Scripted replacement for claude_agent_sdk.query. Each step: str reply, an Exception,
    or a callable(prompt, options) -> list of messages."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls: list[tuple[str, object]] = []

    def __call__(self, prompt, options):
        self.calls.append((prompt, options))
        step = self.steps.pop(0) if self.steps else "ok"

        async def gen():
            if isinstance(step, Exception):
                raise step
            msgs = step(prompt, options) if callable(step) else [SystemMessage(subtype="init", data={"session_id": "sess-1"}),
                                                                 result(step)]
            for m in msgs:
                yield m
        return gen()


@pytest.fixture
def make_orch(base, monkeypatch):
    def factory(*, github=False, multi=False, **cfg_overrides):
        rec = Recorder()
        root = base / "master" if multi else None
        cfg = orch_mod.Config(
            workspace=(root / "projects" / "app") if multi else base / "ws", root=root,
            data_dir=base / "data", template_dir=KIT / "workspace-template",
            pm_prompt_file=KIT / "bot/prompts/pm.md", studio_dir=base / "studio",
            studio_template=KIT / "studio-template", curator_worktree=base / "curator-wt",
            pipeline_poll_s=0, max_rounds=cfg_overrides.pop("max_rounds", 1), **cfg_overrides)
        if github:
            bare = base / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
            cfg.github_repo, cfg.git_remote_url = "owner/app", str(bare)
        o = orch_mod.Orchestrator(cfg, rec)
        o.rec = rec
        return o
    monkeypatch.setenv("PATH", f"{FAKES}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_LOG", str(base / "gh.log"))
    return factory


def fake_claude(monkeypatch, *steps, module=orch_mod) -> FakeClaude:
    fc = FakeClaude(*steps)
    monkeypatch.setattr(module, "query", fc)
    return fc


def green_verify(o):
    (o.cfg.workspace / "scripts/verify.sh").write_text('echo "::: backend: tests OK"; echo "VERIFY: PASS"')


def red_verify(o):
    (o.cfg.workspace / "scripts/verify.sh").write_text('echo "::: backend: tests FAILED"; echo "VERIFY: FAIL (backend: tests)"; exit 1')


def rate_limited(resets_in=1):
    import time
    from claude_agent_sdk.types import RateLimitInfo

    def step(prompt, options):
        info = RateLimitInfo(status="rejected", resets_at=int(time.time()) + resets_in, rate_limit_type="five_hour",
                             utilization=1.0, overage_status=None, overage_resets_at=None,
                             overage_disabled_reason=None, raw={})
        return [RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"), result("limit", error=True)]
    return step
