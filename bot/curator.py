"""Knowledge curator: proposes playbook/lesson improvements; the owner approves them.

Safety model
- Runs only when the team is idle, in a separate git worktree (the team's files are
  never touched while it works).
- The bot (not the agent) commits the result, then enforces: only allowed paths
  changed, size limits respected. Anything else -> rejected automatically.
- Nothing reaches main until the owner sends /learn approve. /learn undo reverts it.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

ALLOWED_PREFIXES = (".claude/skills/",)
ALLOWED_FILES = ("docs/LESSONS.md", "docs/STANDARDS.md", "design/TRENDS.md")


class Curator:
    def __init__(self, orch: "Orchestrator"):
        self.o = orch
        self.cfg = orch.cfg
        # Agent-writable location (the data dir is bot-only)
        self.wt = self.cfg.curator_worktree or (
            sandbox.agent_home() / "curator-worktree" if sandbox.separated() else self.cfg.data_dir / "curator-worktree")
        self.running = False
        self.base = "HEAD"
        self.scheduler: asyncio.Task | None = None

    # ------------------------------------------------------------- helpers
    @property
    def st(self):
        return self.o.state

    def _git(self, *args: str, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
        # Always as the agent user: the repo is agent-controlled (hooks/config could run code).
        return sandbox.git(*args, cwd=cwd or self.cfg.master, check=check)   # team files live in the master

    def _now_local(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg.timezone))

    def _team_idle(self) -> bool:
        return (not (self.o.build_task and not self.o.build_task.done()) and not self.o.lock.locked()
                and not self.o.studio_ops.lock.locked())

    def _cleanup_worktree(self, branch: str | None = None) -> None:
        if self.wt.exists():
            self._git("worktree", "remove", "--force", str(self.wt))
            shutil.rmtree(self.wt, ignore_errors=True)
        self._git("worktree", "prune")
        if branch:
            self._git("branch", "-D", branch)

    # ----------------------------------------------------------- scheduler
    def start_scheduler(self) -> None:
        if self.scheduler is None or self.scheduler.done():
            self.scheduler = asyncio.create_task(self._schedule_loop())

    async def _schedule_loop(self) -> None:
        hh, mm = (int(x) for x in self.cfg.curator_time.split(":"))
        while True:
            try:
                now = self._now_local()
                last = datetime.fromtimestamp(self.st.curator_last_run, ZoneInfo(self.cfg.timezone)) \
                    if self.st.curator_last_run else None
                due = (now.hour, now.minute) >= (hh, mm) and (last is None or last.date() < now.date())
                if (self.st.curator_enabled and due and not self.running
                        and not self.st.curator_pending and self._team_idle()):
                    await self.run(scheduled=True)
            except Exception as e:  # never let the scheduler die
                self.o._log(f"CURATOR scheduler error {e!r}")
            await asyncio.sleep(60)

    # --------------------------------------------------------------- digest
    def _digest(self) -> tuple[str, bool]:
        since = self.st.curator_last_run or (time.time() - 86400)
        since_iso = datetime.fromtimestamp(since).isoformat(timespec="seconds")
        log = self._git("log", f"--since={since_iso}", "--stat", "--format=%n### %h %s", "-n", "40").stdout.strip()
        lines = []
        if self.o.log_file.exists():
            cutoff = datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S")
            for line in self.o.log_file.read_text(errors="replace").splitlines():
                if line[:19] < cutoff:
                    continue
                body = line[20:]
                if (body.startswith(("VERIFY ok=False", "PUSH FAILED", "ERROR", "SESSION LOST"))
                        or ("CHANGES_REQUESTED" in body or "[blocker]" in body)
                        or (body.startswith("PROMPT") and "[BUILD LOOP" not in body)):
                    lines.append(line)
        pipeline_notes = [m for m in self.st.inbox if "pipeline FAILED" in m]
        active = bool(log or lines or pipeline_notes)
        digest = ("## Commits since last run\n" + (log or "(none)")[:6000]
                  + "\n\n## Problems, review findings and owner messages\n"
                  + ("\n".join(lines[-80:]) or "(none)")[:5000]
                  + ("\n\n## Pending pipeline failures\n" + "\n".join(pipeline_notes) if pipeline_notes else ""))
        return digest, active

    # ------------------------------------------------------------------ run
    async def run(self, scheduled: bool = False) -> str:
        if self.running:
            return "The curator is already running."
        if self.st.curator_pending:
            return "There's already a proposal waiting. /learn diff, /learn approve or /learn reject first."
        if not self._team_idle():
            return "The team is busy. The curator only runs while the team is idle."
        digest, active = self._digest()
        if scheduled and not active:
            self.st.curator_last_run = time.time()
            self.o._save_state()
            self.o._log("CURATOR skipped – no activity")
            return "Nothing happened since the last run."

        self.running = True
        stamp = self._now_local().strftime("%Y%m%d-%H%M")
        branch = f"curator/{stamp}"
        try:
            self._cleanup_worktree()
            self.wt.mkdir(parents=True, exist_ok=True)   # empty dir the agent user owns
            sandbox.give_to_agent(self.wt)
            self.base = self._git("rev-parse", "HEAD").stdout.strip()
            r = self._git("worktree", "add", "-b", branch, str(self.wt), self.base)
            if r.returncode != 0:
                return f"Couldn't prepare the curator's copy: {r.stderr.strip()[:300]}"
            if scheduled:
                await self.o.notify("🌙 Knowledge curator started (the team is idle).")
            self.o._log(f"CURATOR start {branch}")
            async with self.o.ops.run("the knowledge curator"):
                summary = await self._ask_agent(digest)
                verdict = self._validate_and_commit(branch)
            self.st.curator_last_run = time.time()
            if verdict["status"] == "empty":
                self._cleanup_worktree(branch)
                self.o._save_state()
                msg = "🌙 Curator: no changes proposed.\n" + _tail(summary, 600)
                await self.o.notify(msg)
                return msg
            if verdict["status"] == "rejected":
                self._cleanup_worktree(branch)
                self.o._save_state()
                msg = f"🛑 Curator proposal rejected automatically: {verdict['reason']}"
                self.o._log(f"CURATOR auto-rejected {verdict['reason']}")
                await self.o.notify(msg)
                return msg
            self.st.curator_pending = {"branch": branch, "summary": summary[:3000],
                                       "files": verdict["files"], "created": time.time()}
            self.o._save_state()
            msg = ("🧠 Curator proposal (nothing changes until you approve):\n\n"
                   f"{_tail(summary, 2500)}\n\nFiles: {', '.join(verdict['files'])}\n\n"
                   "/learn diff to see exact changes · /learn approve · /learn reject")
            await self.o.notify(msg, buttons=[[("👀 Diff", "learn:diff"), ("✅ Approve", "learn:approve"),
                                               ("🗑 Reject", "learn:reject")]])
            return msg
        except asyncio.CancelledError:
            self._cleanup_worktree(branch)
            raise
        except Exception as e:
            self.o._log(f"CURATOR error {e!r}")
            self._cleanup_worktree(branch)
            msg = f"⚠️ Curator failed: {e}"
            await self.o.notify(msg)
            return msg
        finally:
            self.running = False

    async def _ask_agent(self, digest: str) -> str:
        instructions = (Path(__file__).parent / "prompts" / "curator.md").read_text() \
            .replace("{max_skill_lines}", str(self.cfg.max_skill_lines)) \
            .replace("{max_lessons_lines}", str(self.cfg.max_lessons_lines))
        options = ClaudeAgentOptions(
            max_buffer_size=sandbox.SDK_MAX_BUFFER,   # images read by agents can exceed the 1 MB default
            system_prompt={"type": "preset", "preset": "claude_code", "append": instructions},
            cwd=str(self.wt),
            setting_sources=["project"],
            permission_mode="bypassPermissions",
            mcp_servers=self.o.extra_mcp_servers(),
            user=sandbox.agent_user(),
            env=sandbox.agent_env(),
            max_turns=self.cfg.curator_max_turns,
            model=self.cfg.model,
        )
        prompt = ("Run your curation pass now. Digest of what happened since your last run:\n\n"
                  + digest + "\n\nRemember: only edit the allowed files, don't commit, end with the summary format.")
        final, last_text = "", ""
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage):
                for b in msg.content:
                    if isinstance(b, TextBlock) and b.text.strip():
                        last_text = b.text
            elif isinstance(msg, ResultMessage):
                final = msg.result or last_text
        return (final or last_text or "(no summary)").strip()

    def _validate_and_commit(self, branch: str) -> dict:
        wt = self.wt
        self._git("add", "-A", cwd=wt)
        # staged changes vs. the starting point: also catches commits the agent made despite instructions
        changed = [p for p in self._git("diff", "--cached", "--name-only", self.base, cwd=wt).stdout.splitlines() if p]
        if not changed:
            return {"status": "empty"}
        bad = [p for p in changed if not (p.startswith(ALLOWED_PREFIXES) or p in ALLOWED_FILES)]
        if bad:
            return {"status": "rejected", "reason": f"it touched files it may not change: {', '.join(bad[:5])}"}
        for p in changed:
            f = wt / p
            if f.is_symlink():
                return {"status": "rejected", "reason": f"{p} is a symbolic link"}
            if not f.exists():
                if p in ALLOWED_FILES or p.endswith("SKILL.md"):
                    return {"status": "rejected", "reason": f"it deleted {p}"}
                continue
            n = len(f.read_text(errors="replace").splitlines())
            limit = self.cfg.max_lessons_lines if p in ALLOWED_FILES else self.cfg.max_skill_lines
            if n > limit:
                return {"status": "rejected", "reason": f"{p} has {n} lines (limit {limit})"}
            if p.endswith("SKILL.md") and not f.read_text().startswith("---\nname:"):
                return {"status": "rejected", "reason": f"{p} lost its name/description header"}
        r = self._git("commit", "-q", "-m", f"Curator proposal {branch.split('/')[-1]}", cwd=wt)
        if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
            return {"status": "rejected", "reason": f"commit failed: {(r.stderr or r.stdout)[:200]}"}
        return {"status": "ok", "files": changed}

    # ------------------------------------------------------------- commands
    def diff(self) -> str:
        p = self.st.curator_pending
        if not p:
            return "No proposal waiting."
        d = self._git("diff", f"main...{p['branch']}").stdout
        return d[:12000] + ("\n…(truncated)" if len(d) > 12000 else "") if d else "(empty diff)"

    def approve(self) -> str:
        p = self.st.curator_pending
        if not p:
            return "No proposal waiting."
        busy = self.o.ops.busy()
        if busy:
            return busy
        dirty = self._git("status", "--porcelain", "--", ".claude/skills", "docs/LESSONS.md").stdout.strip()
        if dirty:
            return "The playbooks have uncommitted changes in the project right now. Try again when the team is idle."
        r = self._git("merge", "--no-ff", "--no-edit", "-m", f"Approve curator proposal {p['branch']}", p["branch"])
        if r.returncode != 0:
            self._git("merge", "--abort")
            return ("⚠️ Couldn't apply it cleanly (the lessons file changed since the proposal). "
                    "I kept the proposal. /learn reject, then /learn now for a fresh one.")
        self.st.curator_last_merge = self._git("rev-parse", "HEAD").stdout.strip()
        self.st.curator_pending = None
        self.st.curator_approved = getattr(self.st, "curator_approved", 0) + 1
        self.o._save_state()
        self._cleanup_worktree(p["branch"])
        self.o._log(f"CURATOR approved {p['branch']}")
        return "✅ Applied. The team uses the updated playbooks from its next task. (/learn undo reverts it.)"

    def reject(self) -> str:
        p = self.st.curator_pending
        if not p:
            return "No proposal waiting."
        self._cleanup_worktree(p["branch"])
        self.st.curator_pending = None
        self.o._save_state()
        self.o._log(f"CURATOR rejected {p['branch']}")
        return "🗑 Proposal discarded. Nothing changed."

    def undo(self) -> str:
        sha = self.st.curator_last_merge
        if not sha:
            return "There's no approved curator change to undo."
        busy = self.o.ops.busy()
        if busy:
            return busy
        r = self._git("revert", "-m", "1", "--no-edit", sha)
        if r.returncode != 0:
            self._git("revert", "--abort")
            return f"⚠️ Couldn't revert automatically: {(r.stderr or r.stdout).strip()[:300]}"
        self.st.curator_last_merge = None
        self.o._save_state()
        self.o._log(f"CURATOR undo {sha[:12]}")
        return "↩️ Reverted the last approved curator change."

    def set_enabled(self, on: bool) -> str:
        self.st.curator_enabled = on
        self.o._save_state()
        return (f"🌙 Nightly curator ON (runs around {self.cfg.curator_time} when the team is idle, propose-only)."
                if on else "Nightly curator OFF. /learn now still runs it on demand.")

    def stats(self) -> str:
        m = self.st.metrics or {}
        today = self._now_local().date()

        def window(start: int, end: int) -> tuple[int, int]:
            rounds = red = 0
            for i in range(start, end):
                d = m.get((today - timedelta(days=i)).isoformat(), {})
                rounds += d.get("rounds", 0)
                red += d.get("red", 0)
            return rounds, red

        r1, b1 = window(0, 7)
        r2, b2 = window(7, 14)
        pct = lambda r, b: f"{(100 * b / r):.0f}% failing" if r else "no rounds"
        return ("📊 Build rounds where tests failed afterwards:\n"
                f"• last 7 days: {b1}/{r1} ({pct(r1, b1)})\n"
                f"• 7 days before: {b2}/{r2} ({pct(r2, b2)})\n"
                f"Curator proposals approved: {getattr(self.st, 'curator_approved', 0)} · nightly: "
                f"{'on' if self.st.curator_enabled else 'off'}\n"
                "If this doesn't improve after a few weeks of approvals, turn it off with /learn off.")

    def status(self) -> str:
        p = self.st.curator_pending
        state = "running now" if self.running else ("proposal waiting" if p else "idle")
        return (f"🧠 Curator: {state} · nightly {'on' if self.st.curator_enabled else 'off'} "
                f"at ~{self.cfg.curator_time} ({self.cfg.timezone})\n"
                "/learn now · /learn diff · /learn approve · /learn reject · /learn undo · /learn stats · /learn off|on")


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "…" + text[-n:]
