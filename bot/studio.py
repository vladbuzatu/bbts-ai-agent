"""Studio: on-demand agents for one-off jobs (analyses, mockups, proposals, offers).

- Lives in /studio (its own volume): agents/, brand/, jobs/<id>/{input,research,mockups,output}.
  Not in git, separate from the build team's project.
- One job turn at a time, and never while the build team is busy (shared lock).
- While a job is "active", the owner's messages and files go to it; /pm switches back.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable
from zoneinfo import ZoneInfo

from claude_agent_sdk import AgentDefinition, ClaudeAgentOptions, create_sdk_mcp_server, tool

import sandbox
from lab import CarStageLab
from lab import make_tools as make_lab_tools
from lab import new_round as lab_new_round

if TYPE_CHECKING:
    from orchestrator import Orchestrator

SendFile = Callable[[Path, str], Awaitable[None]]

# Fingerprints of earlier kit versions of studio agents/playbooks: if a file in /studio still
# matches one exactly (the owner never edited it), it is upgraded to the new version.
KNOWN_OLD_VERSIONS = {
    "agents/mockup-designer.md": {"ff9e659369deea3df8188a71fef7a900b707e74d806abef81051f3b1636e8bdf"},
    "agents/project-proposal.md": {"6577be7fa41311de38080712d70ead2934ee0d9422188ecf4048d9326ddd55c1"},
    "agents/model-lab.md": {"ae4a4d74e1bfacc21692a458ee6e6b8f9c295db28fbd45aa8b93594b1e611744",
                            "99b3636b1e789ccbe6d9cedf557ef2453ca603242206c1b7f1d35fdcfeb23ab5"},
}
JOB_SUBDIRS = ("input", "research", "mockups", "output")
SLUG_RE = re.compile(r"[^a-z0-9]+")


def parse_agent_file(path: Path) -> dict | None:
    """Front matter (name, description, model) + body. Returns None if malformed."""
    text = path.read_text(errors="replace")
    if not text.startswith("---"):
        return None
    try:
        _, head, body = text.split("---", 2)
    except ValueError:
        return None
    meta = {}
    for line in head.strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    if not meta.get("name") or not meta.get("description"):
        return None
    return {"name": meta["name"], "description": meta["description"],
            "model": meta.get("model") if meta.get("model") not in (None, "", "inherit") else None,
            "prompt": body.strip(), "path": path}


class Studio:
    def __init__(self, orch: "Orchestrator", root: Path, template: Path, send_file: SendFile | None):
        self.o = orch
        self.root = root
        self.template = template
        self.send_file = send_file
        self.busy = False
        self.task: asyncio.Task | None = None
        self.lab = CarStageLab(float(os.environ.get("LAB_BUDGET_USD", "5") or 5))
        self._seed()

    # --------------------------------------------------------------- setup
    def _seed(self) -> None:
        for d in ("agents", "brand", "jobs"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        self._seeded_owner = True
        if self.template.exists():
            import hashlib
            for f in self.template.rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(self.template))
                    dest = self.root / rel
                    if not dest.exists():          # never overwrite the owner's edits...
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, dest)
                    elif hashlib.sha256(dest.read_bytes()).hexdigest() in KNOWN_OLD_VERSIONS.get(rel, ()):
                        shutil.copy2(f, dest)      # ...unless it is an untouched older kit version

    @property
    def st(self) -> dict:
        s = self.o.state.studio
        s.setdefault("active", None)
        s.setdefault("jobs", {})
        return s

    # -------------------------------------------------------------- agents
    def agents(self) -> dict[str, dict]:
        out = {}
        for f in sorted((self.root / "agents").glob("*.md")):
            a = parse_agent_file(f)
            if a:
                out[a["name"]] = a
        return out

    def list_agents_text(self) -> str:
        ags = self.agents()
        if not ags:
            return "No studio agents yet. Add .md files to /studio/agents/."
        lines = ["🎛 Studio agents (start one with /run <name> <brief>):", ""]
        for a in ags.values():
            lines.append(f"• {a['name']}: {a['description']}")
        lines += ["", "Example: /run project-proposal https://exemplu.ro mobile-first, budget ~10k EUR"]
        return "\n".join(lines)

    # ---------------------------------------------------------------- jobs
    def _job(self, job_id: str | None = None) -> dict | None:
        jid = job_id or self.st["active"]
        return self.st["jobs"].get(jid) if jid else None

    def active_job(self) -> dict | None:
        return self._job()

    def job_dir(self, job: dict) -> Path:
        return Path(job["dir"])

    def _new_job(self, agent: str, brief: str) -> dict:
        now = datetime.now(ZoneInfo(self.o.cfg.timezone))
        words = SLUG_RE.sub("-", re.sub(r"https?://(www\.)?", "", brief.lower())).strip("-")
        slug = "-".join(words.split("-")[:4])[:40] or "job"
        jid = f"{now:%Y%m%d-%H%M}-{agent}"[:60]
        if jid in self.st["jobs"]:
            jid += f"-{int(time.time()) % 1000}"
        d = self.root / "jobs" / f"{jid}-{slug}"
        for sub in JOB_SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
        (d / "BRIEF.md").write_text(f"# Brief\n\nAgent: {agent}\nCreated: {now:%Y-%m-%d %H:%M}\n\n{brief}\n")
        sandbox.give_to_agent(d)
        job = {"id": jid, "agent": agent, "dir": str(d), "session": None, "title": brief[:80],
               "created": time.time(), "status": "open"}
        self.st["jobs"][jid] = job
        self.st["active"] = jid
        self.o._save_state()
        return job

    def _target(self, job: dict):
        from orchestrator import TurnTarget

        def set_sid(sid):
            job["session"] = sid
            self.o._save_state()

        return TurnTarget(
            label=f"studio:{job['agent']}",
            get_sid=lambda: job.get("session"),
            set_sid=set_sid,
            options=lambda: self._options(job),
            lost_note=("(Note: your earlier conversation for this job was lost after a restart. "
                       "Re-read BRIEF.md and the files in this job folder to catch up.)\n\n"),
        )

    def _options(self, job: dict) -> ClaudeAgentOptions:
        ags = self.agents()
        lead = ags.get(job["agent"])
        jd = self.job_dir(job).resolve()
        base = (Path(__file__).parent / "prompts" / "studio.md").read_text().replace("{job_dir}", str(jd))
        role = f"\n\n# Your role: {lead['name']}\n\n{lead['prompt']}" if lead else ""
        helpers = {n: AgentDefinition(description=a["description"], prompt=a["prompt"], model=a["model"])
                   for n, a in ags.items() if n != job["agent"]}
        notify, send_file, root = self.o.notify, self.send_file, self.root.resolve()

        @tool("send_file_to_owner", "Send a file from this job folder (or /studio/brand) to the owner on "
              "Telegram. path: relative to the job folder or absolute. caption: one short line.",
              {"path": str, "caption": str})
        async def send_file_to_owner(args):
            p = Path(args["path"])
            p = (p if p.is_absolute() else jd / p).resolve()
            allowed = jd in p.parents or (root / "brand") in p.parents
            if not allowed or not p.is_file():
                return {"content": [{"type": "text", "text": f"Refused: {args['path']} is not a file in this job."}]}
            if p.stat().st_size > 45 * 1024 * 1024:
                return {"content": [{"type": "text", "text": "Refused: file is over Telegram's 50 MB limit."}]}
            if send_file is None:
                return {"content": [{"type": "text", "text": "File sending is not available."}]}
            await send_file(p, args.get("caption", ""))
            self.o._log(f"STUDIO sent {p.name}")
            return {"content": [{"type": "text", "text": f"Sent {p.name}."}]}

        @tool("notify_owner", "Send a short progress message to the owner on Telegram.", {"message": str})
        async def notify_owner(args):
            await notify(f"[{job['agent']}] {args['message']}")
            return {"content": [{"type": "text", "text": "Sent."}]}

        tools = [send_file_to_owner, notify_owner]
        dirs = [str(root / "brand"), str(root / "playbooks")]
        if job["agent"] == "model-lab":
            tools += make_lab_tools(self.lab, lambda: job, self.o._save_state, self.o._log)
            if self.lab.code_dir.is_dir():
                dirs.append(str(self.lab.code_dir))
        server = create_sdk_mcp_server(name="studio", tools=tools)
        return ClaudeAgentOptions(
            max_buffer_size=sandbox.SDK_MAX_BUFFER,   # images read by agents can exceed the 1 MB default
            system_prompt={"type": "preset", "preset": "claude_code", "append": base + role},
            cwd=str(jd),
            add_dirs=dirs,
            permission_mode="bypassPermissions",
            mcp_servers={"studio": server, **self.o.extra_mcp_servers()},
            agents=helpers or None,
            resume=job.get("session"),
            user=sandbox.agent_user(),
            env=sandbox.agent_env(),
            max_turns=self.o.cfg.studio_max_turns,
            model=(lead or {}).get("model") or self.o.cfg.model,
        )

    # --------------------------------------------------------------- turns
    def _blocked_reason(self) -> str | None:
        if self.busy:
            return "A studio job is already working. Wait for its reply (or /job stop)."
        if self.o.parallel:
            return self.o.studio_ops.busy()
        if self.o.build_task and not self.o.build_task.done():
            return "The build team is working. Studio jobs don't run in parallel with it: /stop the build first, or wait."
        return self.o.ops.busy()

    async def start(self, agent: str, brief: str) -> str:
        if agent not in self.agents():
            return f"Unknown studio agent '{agent}'.\n\n" + self.list_agents_text()
        if not brief.strip():
            return f"Tell {agent} what to do, e.g. /run {agent} https://exemplu.ro focus on mobile"
        reason = self._blocked_reason()
        if reason:
            return reason
        job = self._new_job(agent, brief)
        self._spawn(job, f"New job. Brief from the owner:\n\n{brief}")
        return (f"🎛 Started job {job['id']} with {agent}.\nFolder: {job['dir']}\n"
                "I'll message you when it has something. Your messages now go to this job; /pm switches back to the PM.")

    async def message(self, text: str) -> str | None:
        """Route an owner message to the active job. Returns an immediate reply, or None if spawned."""
        job = self.active_job()
        if not job:
            return "No active studio job."
        reason = self._blocked_reason()
        if reason:
            return reason
        if job["agent"] == "model-lab":
            lab_new_round(job)          # each owner message starts a testing round with a fresh budget
        self._spawn(job, text)
        return None

    def _spawn(self, job: dict, prompt: str) -> None:
        self.busy = True
        job["status"] = "working"
        self.o._save_state()
        self.task = asyncio.create_task(self._turn(job, prompt))

    async def _turn(self, job: dict, prompt: str) -> None:
        try:
            async with self.o.studio_ops.run(f"studio job {job['agent']}"):
                reply, ok = await self.o._run_turn(prompt, self._target(job))
            if "NEEDS_INPUT:" in reply:
                job["status"] = "waiting"
                q = reply[reply.index("NEEDS_INPUT:") + len("NEEDS_INPUT:"):].strip()
                await self.o.notify(f"❓ [{job['agent']}] needs your input:\n\n{q}\n\nReply here, or tap below.",
                                    buttons=[[("👍 Use your suggestions", "job:defaults")]])
            else:
                job["status"] = "done" if "DONE:" in reply else ("error" if not ok else "open")
                btns = [[("📁 Files", "job:files"), ("✅ Close job", "job:end")]] if job["status"] == "done" else None
                await self.o.notify(f"🎛 [{job['agent']}]\n\n{_tail(reply, 3000)}", buttons=btns)
        except asyncio.CancelledError:
            job["status"] = "stopped"
            await self.o.notify(f"⏹ [{job['agent']}] stopped.")
            raise
        finally:
            self.busy = False
            self.o._save_state()

    def stop(self) -> bool:
        if self.task and not self.task.done():
            self.task.cancel()
            return True
        return False

    # ---------------------------------------------------------- files in
    def save_upload(self, data: bytes, filename: str, caption: str) -> tuple[Path, str]:
        """Store a file the owner sent. Logo -> brand/, otherwise the active job's input/."""
        safe = re.sub(r"[^\w.\- ]", "_", filename).strip() or "file"
        cap = (caption or "").lower()
        job = self.active_job()
        if "logo" in cap and not job:
            dest = self.root / "brand" / ("logo" + Path(safe).suffix.lower())
        elif job:
            sub = ("stages" if any(w in cap for w in ("stage", "showroom", "background", "fundal")) else
                   "cars" if any(w in cap for w in ("car", "masina", "mașina", "auto")) else "")
            dest = self.job_dir(job) / "input" / sub / safe
        else:
            dest = self.root / "inbox" / safe
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        sandbox.give_to_agent(dest.parent)
        return dest, ("brand" if dest.parent.name == "brand" else ("job" if job else "inbox"))

    # ----------------------------------------------------------- commands
    def jobs_text(self, n: int = 10) -> str:
        jobs = sorted(self.st["jobs"].values(), key=lambda j: j["created"], reverse=True)[:n]
        if not jobs:
            return "No studio jobs yet. /agents to see what you can run."
        icons = {"done": "✅", "waiting": "❓", "working": "⏳", "error": "⚠️", "stopped": "⏹", "ended": "📁"}
        lines = ["Recent jobs (/job open <id> to continue one):"]
        for j in jobs:
            mark = " ← active" if j["id"] == self.st["active"] else ""
            lines.append(f"{icons.get(j['status'], '•')} {j['id']} – {j['title'][:50]}{mark}")
        return "\n".join(lines)

    def job_text(self) -> str:
        j = self.active_job()
        if not j:
            return "No active job. /jobs lists recent ones, /run starts a new one."
        lab = (f"Lab: {int(j.get('lab_runs') or 0)} runs · this round ~${float(j.get('lab_round_spent') or 0):.2f} of "
               f"${float(j.get('lab_budget') or self.lab.budget):.2f} · total ~${float(j.get('lab_spent') or 0):.2f} "
               f"(/lab for details)\n") if j["agent"] == "model-lab" else ""
        return (f"🎛 Active job {j['id']} ({j['agent']}) – {j['status']}\n{j['title']}\nFolder: {j['dir']}\n" + lab +
                "/job files · /job send <file> · /job stop · /job end · /pm to talk to the PM")

    def files_text(self) -> str:
        j = self.active_job()
        if not j:
            return "No active job."
        out = self.job_dir(j) / "output"
        files = sorted(p for p in out.rglob("*") if p.is_file() and "preview" not in p.parts)
        if not files:
            return "No output files yet."
        return "Output files (/job send <name>):\n" + "\n".join(
            f"• {p.relative_to(out)} ({max(1, p.stat().st_size // 1024)} KB)" for p in files)

    async def send_output(self, name: str) -> str:
        j = self.active_job()
        if not j or not name:
            return "Usage: /job send <file name from /job files>"
        out = (self.job_dir(j) / "output").resolve()
        p = (out / name).resolve()
        if out not in p.parents or not p.is_file():
            return f"Not found in this job's output: {name}"
        if self.send_file:
            await self.send_file(p, p.name)
        return f"Sent {p.name}."

    def set_budget(self, usd: str) -> str:
        job = self.active_job()
        if not job:
            return "No active job."
        try:
            value = float(usd.replace("$", "").strip())
        except ValueError:
            return "Usage: /job budget 40   (US dollars, for the active job)"
        if not 0 < value <= 1000:
            return "Pick a budget between 1 and 1000 USD."
        job["lab_budget"] = value
        self.o._save_state()
        return (f"💰 Lab budget for this job: ${value:.2f} per testing round "
                f"(${float(job.get('lab_round_spent') or 0):.2f} used this round, "
                f"${float(job.get('lab_spent') or 0):.2f} in total).")

    def end(self) -> str:
        j = self.active_job()
        if not j:
            return "No active job."
        if j["status"] not in ("done", "error", "stopped"):
            j["status"] = "ended"
        self.st["active"] = None
        self.o._save_state()
        return f"📁 Closed job {j['id']}. Files stay in {j['dir']}. Your messages go to the PM again."

    def open(self, jid: str) -> str:
        matches = [k for k in self.st["jobs"] if k.startswith(jid)] if jid else []
        if len(matches) != 1:
            return "Give a job id from /jobs (the start is enough)."
        self.st["active"] = matches[0]
        self.o._save_state()
        return f"Reopened {matches[0]}. Your messages now go to it; /pm switches back."

    def deactivate(self) -> str:
        if not self.st["active"]:
            return "You're already talking to the PM."
        self.st["active"] = None
        self.o._save_state()
        return "💬 Your messages go to the PM again. (/job open <id> returns to a studio job.)"


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "…" + text[-n:]
