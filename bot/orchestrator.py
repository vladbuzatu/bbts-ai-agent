"""Runs the Project Manager agent (Claude Agent SDK) and the autonomous build loop.

The PM is one long-lived Claude session (resumed by session id). It delegates to
the subagents defined in <workspace>/.claude/agents/*.md. After every build round
this module runs scripts/verify.sh itself, so the agents can't just claim green.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import lab as lab_mod
import sandbox
from projects import ProjectRepos

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

log = logging.getLogger("orchestrator")

Notify = Callable[..., Awaitable[None]]

CONTINUE_PROMPT = """\
[BUILD LOOP – round {round}]
Continue building. Read docs/PROGRESS.md, pick the next unblocked task(s), \
delegate them to the right agents, check the results, update the board, and \
commit finished tasks. Work through as many tasks as you sensibly can this round.
{scope}{extra}
End your reply with a 2–4 line status for the owner. Use `NEEDS_INPUT:` or \
`PROJECT_COMPLETE:` lines as described in your instructions when they apply."""


@dataclass
class Config:
    workspace: Path
    data_dir: Path
    template_dir: Path
    pm_prompt_file: Path
    model: str | None = None
    max_rounds: int = 20
    max_turns_per_round: int = 200
    verify_timeout_s: int = 1800
    # GitHub (optional): "owner/repo" + a fine-grained token for that repo
    github_repo: str | None = None
    github_token: str | None = None
    git_remote_url: str | None = None      # override (tests); default https://github.com/<repo>.git
    staging_url: str | None = None
    production_url: str | None = None
    pipeline_poll_s: int = 30
    pipeline_timeout_s: int = 3600
    # Knowledge curator + Angular tooling
    timezone: str = "Europe/Bucharest"
    curator_time: str = "02:30"
    digest_time: str = "19:00"
    curator_max_turns: int = 80
    max_skill_lines: int = 150
    max_lessons_lines: int = 200
    angular_mcp: bool = True
    # Studio (on-demand agents, outside git)
    studio_dir: Path = Path("/studio")
    studio_template: Path | None = None
    studio_max_turns: int = 300
    curator_worktree: Path | None = None
    # Multi-project: the master folder (team + projects/). None = old single-project layout.
    root: Path | None = None

    @property
    def master(self) -> Path:
        return self.root or self.workspace

    @property
    def multi(self) -> bool:
        return self.root is not None and self.root != self.workspace

    @property
    def github_enabled(self) -> bool:
        return bool(self.github_repo and (self.github_token or self.git_remote_url))

    @classmethod
    def from_env(cls) -> "Config":
        here = Path(__file__).resolve().parent
        root = Path(os.environ.get("WORKSPACE_DIR", "/workspace"))
        repo = (os.environ.get("GITHUB_REPO") or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        main = os.environ.get("MAIN_PROJECT") or (repo.split("/")[-1].lower() if repo else "app")
        multi = os.environ.get("MULTI_PROJECT", "1") != "0"
        return cls(
            workspace=root / "projects" / main if multi else root,
            root=root if multi else None,
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            template_dir=Path(os.environ.get("TEMPLATE_DIR", here.parent / "workspace-template")),
            pm_prompt_file=here / "prompts" / "pm.md",
            model=os.environ.get("CLAUDE_MODEL") or None,
            max_rounds=int(os.environ.get("MAX_BUILD_ROUNDS", "20")),
            max_turns_per_round=int(os.environ.get("MAX_TURNS_PER_ROUND", "200")),
            verify_timeout_s=int(os.environ.get("VERIFY_TIMEOUT_S", "1800")),
            github_repo=(os.environ.get("GITHUB_REPO") or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/") or None,
            github_token=sandbox.secret("GITHUB_TOKEN"),
            staging_url=os.environ.get("STAGING_URL") or None,
            production_url=os.environ.get("PRODUCTION_URL") or None,
            timezone=os.environ.get("TZ") or "Europe/Bucharest",
            curator_time=os.environ.get("CURATOR_TIME", "02:30"),
            digest_time=os.environ.get("DIGEST_TIME", "19:00"),
            angular_mcp=os.environ.get("ANGULAR_MCP", "1") != "0",
            studio_dir=Path(os.environ.get("STUDIO_DIR", "/studio")),
            studio_template=here.parent / "studio-template",
        )


class Ops:
    """One traffic controller for everything that touches the project or runs agents.
    Exclusive operations run one at a time; commands that would collide get a clear
    "busy" reply instead of racing. Read-only commands (/status, /log, /jobs) never wait."""

    def __init__(self, lock: asyncio.Lock):
        self.lock = lock
        self.label: str | None = None
        self.since = 0.0

    def busy(self) -> str | None:
        if not self.lock.locked():
            return None
        mins = int((time.time() - self.since) // 60)
        took = f", {mins} min so far" if mins else ""
        return f"⏳ Busy: {self.label or 'another operation'}{took}. Try again when it's done (or /stop)."

    def run(self, label: str):
        ops = self

        class _Ctx:
            async def __aenter__(self):
                await ops.lock.acquire()
                ops.label, ops.since = label, time.time()
                return ops

            async def __aexit__(self, *exc):
                ops.label = None
                ops.lock.release()
                return False
        return _Ctx()


@dataclass
class TurnTarget:
    """Which conversation a turn belongs to (the PM, or a studio job)."""
    label: str
    get_sid: Callable[[], str | None]
    set_sid: Callable[[str | None], None]
    options: Callable[[], ClaudeAgentOptions]
    lost_note: str = ""


@dataclass
class State:
    session_id: str | None = None
    awaiting_input: bool = False
    last_verify: str = "not run yet"
    rounds_done: int = 0
    inbox: list[str] = field(default_factory=list)
    curator_enabled: bool = True
    curator_last_run: float = 0.0
    curator_pending: dict | None = None
    curator_last_merge: str | None = None
    curator_approved: int = 0
    last_pushed: str | None = None
    changes_seen: str | None = None
    usage: dict = field(default_factory=dict)
    digest_enabled: bool = True
    last_digest: str | None = None
    live_msg_id: int | None = None
    metrics: dict = field(default_factory=dict)
    studio: dict = field(default_factory=lambda: {"active": None, "jobs": {}})
    pm_lab: dict = field(default_factory=dict)      # the PM's own CarStage lab ledger (runs, spend)
    main_project: str | None = None                 # name of the main project (multi-project layout)
    build_scope: list = field(default_factory=list) # projects the running build covers
    projects: dict = field(default_factory=dict)    # other projects in the workspace: name -> {path, title}
    active_project: str | None = None               # what /status shows; None = the main project
    lab_code: dict = field(default_factory=dict)    # which code the CarStage lab runs (source, sha, when)


class Orchestrator:
    def __init__(self, cfg: Config, notify: Notify,
                 send_image: Callable[[Path, str], Awaitable[None]] | None = None,
                 send_file: Callable[[Path, str], Awaitable[None]] | None = None):
        self.cfg = cfg
        self.notify = notify
        self.send_image = send_image
        self.send_file = send_file
        self.lock = asyncio.Lock()          # exclusive operations (see Ops)
        self.ops = Ops(self.lock)
        self.build_task: asyncio.Task | None = None
        self.current_turn: asyncio.Task | None = None
        sandbox.lock_down(self.cfg.data_dir)   # bot-only: state, log, git mirror
        self.state_file = self.cfg.data_dir / "state.json"
        self.log_file = self.cfg.data_dir / "agent.log"
        self.state = self._load_state()
        self.watchers: set[asyncio.Task] = set()
        self.preview_procs: dict[str, asyncio.subprocess.Process] = {}
        import layout
        layout.migrate_to_master(self)
        self._init_workspace()
        self._sync_infra()
        self._migrate_agent_models()
        self._migrate_playbooks()
        self._migrate_tools()
        self._migrate_status_v1()
        self._migrate_once("no-github-for-agents-v1", "no_github_for_agents",
                           "Agents use CI logs from the bot instead of GitHub access")
        if self._migrate_once("agents-v2", "agents_v2", "Design direction process, art director, standards register",
                              self.cfg.template_dir):
            self.state.inbox.append(
                "Team upgrade: (1) new `art-director` agent for independent design critique; (2) ui-designer now "
                "follows `.claude/skills/design-direction/SKILL.md` (brand brief, three directions for the owner to "
                "choose, banned generic defaults, a signature element); (3) `docs/STANDARDS.md` lists the standards "
                "we follow (WCAG 2.2 AA, OWASP ASVS 5.0 L2, OWASP Top 10:2025, Core Web Vitals) and how each is "
                "checked; (4) new playbooks: accessibility, web-performance, security-review. Apply them to new and "
                "changed work. If the current design was made before this upgrade, ask the owner whether to run "
                "a direction exploration for it (don't redo it unasked).")
            self._save_state()
        self._migrate_once("verify-loud-v1", "verify_fail_loudly", "verify.sh: fail when backend dependencies can't be installed")
        self._migrate_once("no-watch-v1", "no_watch_rule", "CLAUDE.md: never run tests in watch mode or servers in the foreground")
        if self._migrate_once("secure-deploy-v1", "secure_deploy_v1",
                              "Deploy through the server's deploy gate (rollback, pinned host key)",
                              self.cfg.template_dir):
            self.state.inbox.append(
                "The deploy files (deploy/, .github/workflows/deploy-*.yml) were replaced by the owner's kit with a "
                "gate-based version: deploys go through /opt/app/bin/deploy-gate on the server, which rolls back "
                "automatically. Don't change deploy/deploy.sh, deploy/deploy-gate, deploy/server-setup.sh or the "
                "deploy workflows without asking the owner: they are security-critical.")
            self._save_state()
        layout.init_master(self)
        self._register_main_project()
        from curator import Curator
        self.curator = Curator(self)
        from studio import Studio
        tpl = self.cfg.studio_template or (Path(__file__).resolve().parent.parent / "studio-template")
        self.studio = Studio(self, self.cfg.studio_dir, tpl, send_file)
        self.repos = ProjectRepos(self)
        self.studio.lab.before_run = self.repos.refresh_lab          # the lab follows GitHub main
        self.studio.lab.code_version = lambda: self.state.lab_code
        sandbox.give_to_agent(self.cfg.master, self.cfg.studio_dir)
        self._setup_git()

    # ------------------------------------------------------------------ setup
    INFRA_PATHS = (".github", "deploy", ".claude/agents/devops-engineer.md", "docs/DEPLOYMENT.md",
                   ".claude/skills", "docs/LESSONS.md", ".claude/agents/art-director.md",
                   "docs/STANDARDS.md", "design/TRENDS.md", "design/REFERENCES.md", "design/BRIEF.md")

    def _sync_infra(self) -> None:
        """Add CI/CD + deployment files from the template to an existing project (never overwrites)."""
        ws, tpl = self.cfg.workspace, self.cfg.template_dir
        added: list[str] = []
        import layout
        team = tuple(layout.TEAM_PATHS) + (".claude/agents/devops-engineer.md", ".claude/agents/art-director.md")
        for rel in self.INFRA_PATHS:
            if self.cfg.multi and rel.startswith(team):
                continue          # team files live in the master folder (layout.init_master)
            src = tpl / rel
            files = [src] if src.is_file() else (sorted(p for p in src.rglob("*") if p.is_file()) if src.exists() else [])
            for f in files:
                dest = ws / f.relative_to(tpl)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    added.append(str(f.relative_to(tpl)))
        for sh in (ws / "deploy").glob("*.sh"):
            sh.chmod(0o755)
        if not added:
            return
        log.info("Added infra files: %s", added)
        sandbox.give_to_agent(ws)
        sandbox.git("add", "--", *added, cwd=ws)
        sandbox.git("commit", "-q", "-m", "Add CI/CD pipeline and deployment files", "--", *added, cwd=ws)
        self.state.inbox.append(
            "The project now has a CI/CD pipeline and a new team member, `devops-engineer` "
            "(see docs/DEPLOYMENT.md, .github/workflows/, deploy/). Add a devops task to the board: "
            "once backend and frontend exist, make the app meet docs/DEPLOYMENT.md. Also add to CLAUDE.md "
            "that agents commit but never push or deploy.")
        self._save_state()

    SONNET_AGENTS = ("backend-dev", "frontend-dev", "qa-engineer", "devops-engineer")

    def _migrate_agent_models(self) -> None:
        """One-time: move developer/QA/devops agents from 'inherit' to Sonnet (faster, cheaper)."""
        marker = self.cfg.data_dir / "migrated-sonnet-agents"
        if marker.exists():
            return
        changed = []
        for name in self.SONNET_AGENTS:
            f = self.cfg.workspace / ".claude" / "agents" / f"{name}.md"
            if f.exists():
                text = f.read_text()
                new = text.replace("\nmodel: inherit\n", "\nmodel: sonnet\n", 1)
                if new != text:
                    f.write_text(new)
                    changed.append(str(f.relative_to(self.cfg.workspace)))
        if changed:
            sandbox.give_to_agent(self.cfg.workspace)
            sandbox.git("add", "--", *changed, cwd=self.cfg.workspace)
            sandbox.git("commit", "-q", "-m", "Run developer, QA and devops agents on Sonnet", "--", *changed,
                        cwd=self.cfg.workspace)
        marker.write_text("done\n")

    def _migrate_playbooks(self) -> None:
        """One-time: point existing agents at the playbooks and lessons file."""
        marker = self.cfg.data_dir / "migrated-playbooks-v1"
        if marker.exists():
            return
        import upgrades
        changed = upgrades.link_playbooks(self.cfg.workspace)
        upgrades.commit(self.cfg.workspace, changed, "Link agents to Angular 22 / Django 6.1 playbooks and lessons")
        if changed:
            self.state.inbox.append(
                "The team now has playbooks in .claude/skills/ (angular-components, angular-forms, angular-testing, "
                "django-api, django-security, django-testing) and a docs/LESSONS.md file. Each agent file lists the "
                "playbooks it must follow. When briefing agents, name the relevant playbook. Record lessons as "
                "described in your instructions. Existing code that predates the playbooks doesn't need rewriting; "
                "apply the playbooks to new and changed code.")
            self._save_state()
        marker.write_text("done\n")

    def _migrate_tools(self) -> None:
        marker = self.cfg.data_dir / "migrated-tools-v1"
        if marker.exists():
            return
        import upgrades
        changed = upgrades.fix_tools(self.cfg.workspace)
        upgrades.commit(self.cfg.workspace, changed, "Use the pinned Playwright 1.65 for screenshots and e2e")
        marker.write_text("done\n")

    def _migrate_status_v1(self) -> None:
        marker = self.cfg.data_dir / "migrated-status-v1"
        if marker.exists():
            return
        if (self.cfg.workspace / "docs" / "SPEC.md").exists() and self.state.session_id:
            self.state.inbox.append(
                "The owner's /status is now built from docs/PROGRESS.md and docs/SPEC.md. As your first step: "
                "(1) make sure PROGRESS.md has one table with columns ID | Task | Owner | Status | Depends on, "
                "statuses only todo/in-progress/review/done/blocked, short titles; (2) in SPEC.md tick `- [x]` every "
                "acceptance criterion that is already implemented and covered by passing tests. Commit as "
                "'Tidy task board and plan for /status'.")
            self._save_state()
        marker.write_text("done\n")

    def _migrate_once(self, marker_name: str, fn: str, message: str, *extra) -> list[str]:
        marker = self.cfg.data_dir / f"migrated-{marker_name}"
        if marker.exists():
            return []
        import upgrades
        changed = getattr(upgrades, fn)(self.cfg.workspace, *extra)
        upgrades.commit(self.cfg.workspace, changed, message)
        marker.write_text("done\n")
        return changed

    def extra_mcp_servers(self) -> dict:
        """External tool servers available to every agent."""
        servers: dict = {}
        if self.cfg.angular_mcp and shutil.which("ng"):
            servers["angular-cli"] = {"type": "stdio", "command": "ng",
                                      "args": ["mcp", "--read-only", "--root", str(self.cfg.workspace)]}
        return servers

    def _record_round(self, green: bool) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        day = datetime.now(ZoneInfo(self.cfg.timezone)).date().isoformat()
        d = self.state.metrics.setdefault(day, {"rounds": 0, "red": 0})
        d["rounds"] += 1
        d["red"] += 0 if green else 1
        for k in sorted(self.state.metrics)[:-60]:   # keep ~60 days
            del self.state.metrics[k]
        self._save_state()

    @property
    def mirror(self) -> Path:
        return self.cfg.data_dir / "mirror.git"

    def _setup_git(self) -> None:
        """The bot pushes from its own bare mirror (in the bot-only data dir). The agents'
        folder never has credentials, and root never runs git inside it."""
        if not self.cfg.github_enabled:
            return
        if self.cfg.github_token:
            self.repos.write_credentials()       # one line per repo (each can have its own token)
        url = self.cfg.git_remote_url or f"https://github.com/{self.cfg.github_repo}.git"
        m = self.mirror
        if not (m / "HEAD").exists():
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(m)], check=False)
        has_origin = subprocess.run(["git", "-C", str(m), "remote", "get-url", "origin"],
                                    capture_output=True).returncode == 0
        subprocess.run(["git", "-C", str(m), "remote", "set-url" if has_origin else "add", "origin", url], check=False)
        # Old kit versions stored a remote in the agents' folder: harmless, but not used any more.

    def _init_workspace(self) -> None:
        ws = self.cfg.workspace
        ws.mkdir(parents=True, exist_ok=True)
        if not (ws / "docs" / "PROGRESS.md").exists():
            log.info("Seeding workspace from template")
            shutil.copytree(self.cfg.template_dir, ws, dirs_exist_ok=True)
            (ws / "scripts" / "verify.sh").chmod(0o755)
        sandbox.give_to_agent(ws)
        if not (ws / ".git").exists():
            sandbox.git("init", "-q", "-b", "main", cwd=ws, check=True)
            sandbox.git("add", "-A", cwd=ws, check=True)
            sandbox.git("commit", "-q", "-m", "Initial project scaffold", cwd=ws)

    def _load_state(self) -> State:
        if self.state_file.exists():
            try:
                return State(**json.loads(self.state_file.read_text()))
            except Exception:  # corrupt state: start fresh rather than crash
                log.exception("Could not read state; starting fresh")
        return State()

    def _save_state(self) -> None:
        self.state_file.write_text(json.dumps(self.state.__dict__, indent=2))

    def _log(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.log_file.open("a") as f:
            f.write(f"{stamp} {line}\n")

    def _options(self) -> ClaudeAgentOptions:
        notify = self.notify

        @tool("notify_owner", "Send a short progress message to the owner on Telegram.",
              {"message": str})
        async def notify_owner(args):
            await notify(f"📣 {args['message']}")
            return {"content": [{"type": "text", "text": "Sent."}]}

        workspace = self.cfg.master.resolve()      # screenshots may come from any project
        send_image = self.send_image

        @tool("send_screenshots_to_owner",
              "Send PNG/JPG images (e.g. design mockup screenshots) to the owner on Telegram so they "
              "can see and approve them. paths: image file paths relative to your working folder "
              "(e.g. projects/carstage/design/screens/home.png), "
              "comma-separated (max 10). caption: one short line describing them.",
              {"paths": str, "caption": str})
        async def send_screenshots_to_owner(args):
            if send_image is None:
                return {"content": [{"type": "text", "text": "Image sending is not available."}]}
            sent, problems = 0, []
            for raw in [p.strip() for p in args["paths"].split(",") if p.strip()][:10]:
                path = (workspace / raw).resolve()
                if workspace not in path.parents or not path.is_file():
                    problems.append(f"{raw}: not found in the project")
                    continue
                if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                    problems.append(f"{raw}: not an image")
                    continue
                await send_image(path, f"{args['caption']}\n{raw}" if sent == 0 else raw)
                sent += 1
            msg = f"Sent {sent} image(s)." + (f" Problems: {'; '.join(problems)}" if problems else "")
            return {"content": [{"type": "text", "text": msg}]}

        orch = self

        @tool("set_active_project",
              "Switch the owner's /status to the project you're working on now. Use it whenever the owner "
              "gives you work on a different project. name: short id (e.g. carstage). path: the project's "
              "folder relative to the workspace (e.g. carstage), or '.' for the main project. title: display "
              "name. That folder must have its own docs/PROGRESS.md and docs/SPEC.md (create them first).",
              {"name": str, "path": str, "title": str})
        async def set_active_project(args):
            text = await asyncio.to_thread(orch.set_active_project, args.get("name", ""), args.get("path", ""),
                                           args.get("title", ""))
            return {"content": [{"type": "text", "text": text}]}

        @tool("publish_project",
              "Publish another project's committed work to its GitHub repo: the bot pushes the branch "
              "agents/work and opens/updates a pull request the owner reviews (main never changes without "
              "the owner). Commit first. Not for the main project (the bot pushes that itself).",
              {"name": str, "title": str, "summary": str})
        async def publish_project(args):
            text = await orch.repos.publish(args.get("name", ""), args.get("title", ""), args.get("summary", ""))
            return {"content": [{"type": "text", "text": text}]}

        @tool("update_project_from_github",
              "Bring another project's GitHub main (e.g. after the owner merged a pull request) into its "
              "folder. Commit your work first.", {"name": str})
        async def update_project_from_github(args):
            text = await orch.repos.pull(args.get("name", ""))
            return {"content": [{"type": "text", "text": text}]}

        team_tools = [notify_owner, send_screenshots_to_owner, set_active_project]
        if any(p.get("repo") for p in self.state.projects.values()):
            team_tools += [publish_project, update_project_from_github]
        lab_dirs = []
        if lab_mod.CODE_DIR.is_dir():           # the CarStage lab is installed on this server
            team_tools += lab_mod.make_tools(self.studio.lab, self.pm_lab_job, self._save_state, self._log)
            lab_dirs = [str(self.pm_lab_job()["dir"])] + ([str(lab_mod.CODE_DIR)] if lab_mod.CODE_DIR.is_dir() else [])
        team_server = create_sdk_mcp_server(name="team", tools=team_tools)
        return ClaudeAgentOptions(
            max_buffer_size=sandbox.SDK_MAX_BUFFER,   # images read by agents can exceed the 1 MB default
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": self.cfg.pm_prompt_file.read_text() + self.projects_brief(),
            },
            cwd=str(self.cfg.master),
            add_dirs=lab_dirs,
            setting_sources=["project"],          # loads CLAUDE.md + .claude/agents/*.md
            permission_mode="bypassPermissions",  # safe only because we run in a container
            mcp_servers={"team": team_server, **self.extra_mcp_servers()},
            resume=self.state.session_id,
            user=sandbox.agent_user(),
            env=sandbox.agent_env(),
            max_turns=self.cfg.max_turns_per_round,
            model=self.cfg.model,
        )

    # ------------------------------------------------------------ projects
    @property
    def main_name(self) -> str:
        if self.cfg.multi:
            return self.cfg.workspace.name
        return ((self.cfg.github_repo or "").split("/")[-1] or self.cfg.workspace.name or "main").lower()

    def _register_main_project(self) -> None:
        """Multi-project: the main project is a normal entry in the registry (path under projects/)."""
        if not self.cfg.multi:
            return
        info = self.state.projects.setdefault(self.main_name, {})
        repo_name = (self.cfg.github_repo or "").split("/")[-1] or self.main_name
        info.setdefault("title", repo_name.capitalize() if repo_name.islower() else repo_name)
        info.update({"path": self.cfg.workspace.relative_to(self.cfg.master).as_posix(), "main": True,
                     "repo": self.cfg.github_repo or info.get("repo")})
        info.setdefault("mode", "main")          # push main -> CI -> staging (the original flow)
        self.state.main_project = self.main_name
        if self.state.active_project == self.main_name:
            self.state.active_project = None
        self._save_state()

    def project_names(self) -> list[str]:
        """Main project first, then the others in the order they were added."""
        names = [n for n in self.state.projects if n != self.main_name]
        return [self.main_name] + names

    def project_root(self, name: str | None = None) -> tuple[str, Path]:
        """(title, folder) of a project; the active one when name is None; the main one if unknown."""
        name = self.state.active_project if name is None else name
        info = self.state.projects.get(name or "")
        if info and not info.get("main"):
            base = self.cfg.master if self.cfg.multi else self.cfg.workspace
            return info.get("title") or name, base / info["path"]
        main_info = self.state.projects.get(self.main_name, {})
        title = main_info.get("title") or ((self.cfg.github_repo or "").split("/")[-1] or self.cfg.workspace.name
                                           or "Project")
        return (title.capitalize() if title.islower() else title), self.cfg.workspace

    def projects_brief(self) -> str:
        """Appended to the PM's instructions: where each project lives and how its work ships."""
        if not self.cfg.multi:
            return ""
        lines = ["", "", "## Your projects (multi-project workspace)",
                 f"You work from the master folder {self.cfg.master}. Paths in the instructions above such as "
                 "docs/PROGRESS.md, docs/SPEC.md, scripts/verify.sh and design/ are relative to a PROJECT folder. "
                 "Always brief agents with the project folder. Exceptions (shared by all projects, in the "
                 "master folder): docs/LESSONS.md, docs/STANDARDS.md, design/TRENDS.md, .claude/agents and "
                 ".claude/skills.", ""]
        for name in self.project_names():
            info = self.project_info(name)
            title, folder = self.project_root(name)
            rel = folder.relative_to(self.cfg.master).as_posix() if folder.is_relative_to(self.cfg.master) else str(folder)
            ship = ("commits on main are pushed by the bot after green tests; GitHub CI deploys staging"
                    if info.get("mode") == "main" else
                    "work on a feature branch; after green tests the bot pushes it and opens/updates a pull "
                    "request (the owner merges)") if info.get("repo") else "not linked to GitHub yet (/project link)"
            has_verify = (folder / "scripts" / "verify.sh").exists()
            lines.append(f"- **{name}** ({title}): {rel}/ · plan {rel}/docs/PROGRESS.md · tests "
                         f"{rel + '/scripts/verify.sh' if has_verify else 'MISSING scripts/verify.sh (create it first)'}"
                         f" · {ship}")
        active = self.state.active_project or self.main_name
        lines += ["", f"The owner's /status currently shows: {active}."]
        return "\n".join(lines) + "\n"

    def project_info(self, name: str | None) -> dict:
        name = name or self.main_name
        info = dict(self.state.projects.get(name, {}))
        if name == self.main_name:
            info.setdefault("repo", self.cfg.github_repo)
            info.setdefault("mode", "main")
            info["main"] = True
        return info

    def set_active_project(self, name: str, path: str = "", title: str = "") -> str:
        import re as _re
        name = _re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30]
        path = (path or "").strip().strip("/")
        if name in ("", "main", self.main_name) or (path in ("", ".") and name not in self.state.projects):
            self.state.active_project = None
            self._save_state()
            return f"OK: /status now shows the main project ({self.project_root(self.main_name)[0]})."
        if not path and name in self.state.projects:
            self.state.active_project = name
            self._save_state()
            return f"OK: /status now shows {self.state.projects[name].get('title', name)}."
        base = (self.cfg.master if self.cfg.multi else self.cfg.workspace).resolve()
        folder = (base / path).resolve()
        if folder == base or base not in folder.parents or not folder.is_dir() or (base / path).is_symlink():
            return f"Refused: {path!r} must be an existing folder inside the workspace" + \
                   (" (projects live in projects/<name>)." if self.cfg.multi else ".")
        rel = folder.relative_to(base).as_posix()
        if self.cfg.multi and not rel.startswith("projects/"):
            return f"Refused: projects live in projects/<name>, not {rel!r}."
        if self.cfg.multi and folder == self.cfg.workspace.resolve():
            return self.set_active_project(self.main_name)
        if not self.cfg.multi and rel.split("/")[0] in (".git", ".claude", "docs", "frontend", "backend", "design", "scripts"):
            return f"Refused: {rel!r} is part of the main project."
        missing = [f for f in ("docs/PROGRESS.md", "docs/SPEC.md") if not (folder / f).exists()]
        info = self.state.projects.setdefault(name, {})
        info.update({"path": rel, "title": (title or info.get("title") or name).strip()[:40]})
        info.setdefault("mode", "pr")
        self.state.active_project = name
        self._save_state()
        note = "" if self.cfg.multi else self._keep_out_of_main_repo(rel)
        msg = f"OK: /status now shows {info['title']} ({rel}/docs/PROGRESS.md and SPEC.md)."
        if missing:
            msg += f" Missing: {', '.join(rel + '/' + m for m in missing)} - create them now (same format as the main project)."
        return msg + note

    def _keep_out_of_main_repo(self, rel: str) -> str:
        """Another project must not be committed (and pushed) as part of the main project's repo."""
        ws = self.cfg.workspace
        exclude = ws / ".git" / "info" / "exclude"
        if not exclude.parent.is_dir():
            return ""
        line = f"/{rel}/"
        text = exclude.read_text() if exclude.exists() else ""
        if line not in text.splitlines():
            exclude.write_text(text.rstrip("\n") + ("\n" if text else "") + line + "\n")
            sandbox.give_to_agent(exclude)
        tracked = sandbox.git("ls-files", "--", rel, cwd=ws).stdout.strip()
        if not tracked:
            return f" {rel}/ is kept out of the main project's git repo."
        sandbox.git("rm", "-r", "--cached", "--quiet", "--", rel, cwd=ws)
        sandbox.git("commit", "-m", f"chore: keep {rel}/ out of this repo (separate project)", cwd=ws)
        self._log(f"PROJECT: untracked {rel}/ from the main repo")
        return (f" {rel}/ had been committed to the main project's repo; it is now removed from the repo (files kept)."
                " Earlier commits on GitHub still contain it.")

    def pm_lab_job(self) -> dict:
        """The PM's lab ledger: results in /studio/pm-lab, inputs from there or from the project."""
        d = self.cfg.studio_dir / "pm-lab"
        if not d.is_dir():
            (d / "input").mkdir(parents=True, exist_ok=True)
            sandbox.give_to_agent(d)
        job = self.state.pm_lab
        job.update({"id": "pm", "agent": "pm", "dir": str(d), "input_roots": [str(self.cfg.master)]})
        return job

    # -------------------------------------------------------------- PM turns
    def _pm_target(self) -> "TurnTarget":
        def set_sid(sid):
            self.state.session_id = sid
            self._save_state()
        return TurnTarget(
            label="PM",
            get_sid=lambda: self.state.session_id,
            set_sid=set_sid,
            options=self._options,
            lost_note=("(Note: your previous conversation history was lost after a restart. "
                       "Re-read docs/SPEC.md, docs/PROGRESS.md, docs/DECISIONS.md and git log "
                       "to catch up before answering.)\n\n"),
        )

    async def _run_turn(self, prompt: str, target: "TurnTarget | None" = None) -> tuple[str, bool]:
        """Run one agent turn (the PM by default). Returns (final_text, ok).
        Handles a lost conversation (starts fresh) and usage limits (waits, then continues)."""
        t = target or self._pm_target()
        recovered = False
        while True:
            final, ok, reset_at = await self._run_turn_once(prompt, t)
            if not ok and not recovered and t.get_sid() and "No conversation found" in final:
                self._log(f"SESSION LOST – starting a fresh {t.label} conversation")
                t.set_sid(None)
                recovered = True
                prompt = t.lost_note + prompt
                continue
            if reset_at is None:
                return final, ok
            wait = max(60, reset_at - int(time.time()) + 60)
            when = time.strftime("%H:%M", time.localtime(reset_at))
            await self.notify(f"⏸ Claude usage limit reached. Pausing until about {when}, then continuing automatically.")
            self._log(f"RATE LIMIT – sleeping {wait}s")
            await asyncio.sleep(wait)
            prompt = "Continue where you left off (we paused for a usage limit)."

    async def _run_turn_once(self, prompt: str, t: "TurnTarget") -> tuple[str, bool, int | None]:
        self._log(f"PROMPT[{t.label}] {prompt[:300]!r}" if t.label != "PM" else f"PROMPT {prompt[:300]!r}")
        final, ok, reset_at = "", False, None
        last_text = ""
        try:
            async for msg in query(prompt=prompt, options=t.options()):
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    sid = msg.data.get("session_id")
                    if sid and sid != t.get_sid():
                        t.set_sid(sid)
                elif isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            last_text = block.text
                            self._log(f"TEXT {block.text[:500]!r}")
                        elif isinstance(block, ToolUseBlock):
                            detail = block.input.get("subagent_type") or block.input.get("command") \
                                or block.input.get("file_path") or block.input.get("description") or ""
                            self._log(f"TOOL {block.name} {str(detail)[:200]}")
                elif isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    self.state.usage[info.rate_limit_type or "five_hour"] = {
                        "status": info.status, "utilization": info.utilization, "resets_at": info.resets_at}
                    if info.status == "rejected" and info.resets_at:
                        reset_at = info.resets_at
                elif isinstance(msg, ResultMessage):
                    if msg.session_id:
                        t.set_sid(msg.session_id)
                    final = msg.result or last_text
                    ok = not msg.is_error
                    self._log(f"RESULT ok={ok} turns={msg.num_turns} subtype={msg.subtype}")
        except asyncio.CancelledError:
            self._log("CANCELLED")
            raise
        except Exception as e:  # surface SDK/CLI errors to the owner instead of dying
            log.exception("%s turn failed", t.label)
            self._log(f"ERROR {e!r}")
            return f"Agent error: {e}", False, reset_at
        if reset_at and not ok:
            return final, ok, reset_at
        return final or last_text or "(no reply)", ok, None

    async def chat(self, text: str) -> str:
        """A normal message from the owner to the PM."""
        if self.build_task and not self.build_task.done():
            self.state.inbox.append(text)
            self._save_state()
            return "📥 Build is running. I'll pass this to the PM at the start of the next step."
        resume_build = self.state.awaiting_input
        lab_mod.new_round(self.pm_lab_job())     # each owner message: a fresh lab budget for the PM
        self._save_state()
        async with self.ops.run("a conversation with the PM"):
            self.current_turn = asyncio.current_task()
            reply, _ = await self._run_turn(text)
            self.current_turn = None
        if resume_build:
            self.state.awaiting_input = False
            self._save_state()
            if "NEEDS_INPUT:" not in reply:
                self.start_build(self.state.build_scope or None)
                reply += "\n\n▶️ Resuming the build."
        elif "START_BUILD:" in reply:
            wanted = reply[reply.index("START_BUILD:") + len("START_BUILD:"):].splitlines()[0]
            names = [w.strip(" .,`*").lower() for w in wanted.replace(",", " ").split()]
            names = [] if any(n in ("all", "") for n in names) or not names else names
            reply = reply[:reply.index("START_BUILD:")].rstrip()
            if self.start_build(names or None):
                reply += "\n\n▶️ Build started" + (f": {', '.join(self.state.build_scope)}." if self.cfg.multi else ".")
            else:
                reply += "\n\n(Couldn't start the build: something else is running. Send /build when it's free.)"
        return reply

    # ------------------------------------------------------------ build loop
    def resolve_scope(self, scope: list[str] | None = None) -> list[str]:
        """Which projects a build covers: the ones asked for, else every project with open tasks."""
        if not self.cfg.multi:
            return [self.main_name]
        known = self.project_names()
        if scope:
            picked = [n for n in scope if n in known]
            if picked:
                return picked
        open_ = [n for n in known if self.open_task_count(n) > 0]
        return open_ or [self.state.active_project or self.main_name]

    def start_build(self, scope: list[str] | None = None) -> bool:
        if self.build_task and not self.build_task.done():
            return False
        if getattr(self, "studio", None) and self.studio.busy:
            return False
        self.state.awaiting_input = False
        self.state.build_scope = self.resolve_scope(scope)
        self._save_state()
        self._kill_previews()   # agents may need ports 4200/8000 for their own tests
        self.build_task = asyncio.create_task(self._build_loop())
        return True

    def stop(self) -> bool:
        stopped = False
        for t in (self.build_task, self.current_turn):
            if t and not t.done():
                t.cancel()
                stopped = True
        return stopped

    def _scope_text(self, scope: list[str]) -> str:
        if not self.cfg.multi:
            return ""
        parts = []
        for n in scope:
            title, folder = self.project_root(n)
            rel = folder.relative_to(self.cfg.master).as_posix()
            parts.append(f"- {n} ({title}): plan {rel}/docs/PROGRESS.md, tests {rel}/scripts/verify.sh")
        return ("Projects in this build (docs/... paths above are inside each project folder; pick tasks "
                "across them by priority and dependencies; feature branches for projects that ship by pull "
                "request):\n" + "\n".join(parts) + "\n")

    async def _publish(self, name: str) -> str:
        info = self.project_info(name)
        if not info.get("repo"):
            return ""
        if info.get("mode") == "main" and name == self.main_name:
            return await self.push() if self.cfg.github_enabled else ""
        return await self.repos.publish(name)

    async def _build_loop(self) -> None:
        scope = self.state.build_scope or [self.main_name]
        await self.notify("▶️ Build started" + (f" · {', '.join(scope)}." if self.cfg.multi else "."))
        failing: dict[str, str] = {}
        verify_ok = True
        try:
            for rnd in range(1, self.cfg.max_rounds + 1):
                scope = self.state.build_scope or [self.main_name]
                extra = []
                if self.state.inbox:
                    msgs = "\n".join(f"- {m}" for m in self.state.inbox)
                    extra.append(f"Messages from the owner since last round (handle these first):\n{msgs}")
                    self.state.inbox.clear()
                if not self.cfg.multi and self.state.last_verify not in ("not run yet",) and not verify_ok:
                    failing = {self.main_name: self.state.last_verify}
                for n, out in failing.items():
                    where = f" for {n}" if self.cfg.multi else ""
                    extra.append(f"The independent verify run{where} after the last round FAILED. "
                                 "Fix this before starting new work:\n" + out[-3000:])
                prompt = CONTINUE_PROMPT.format(round=rnd, scope=self._scope_text(scope), extra="\n\n".join(extra))

                # The whole round (agents -> independent tests -> publish) is one exclusive operation.
                async with self.ops.run(f"build round {rnd}" + (f" · {', '.join(scope)}" if self.cfg.multi else "")):
                    self.current_turn = asyncio.current_task()
                    before = {n: self._project_head(n) for n in scope}
                    reply, ok = await self._run_turn(prompt)
                    self.current_turn = None
                    self.state.rounds_done += 1
                    results: dict[str, bool] = {}
                    for n in scope:
                        touched = self._project_head(n) != before[n] or n in failing \
                            or "last_verify" not in self.state.projects.get(n, {"last_verify": None}) \
                            or not self.cfg.multi
                        if touched:
                            results[n], _ = await self.verify(project=n)
                    failing = {n: (self.state.projects.get(n, {}).get("last_verify") or self.state.last_verify)
                               for n, good in results.items() if not good}
                    verify_ok = not failing
                    self._record_round(verify_ok)
                    published = []
                    for n, good in results.items():
                        if good:
                            note = await self._publish(n)
                            if note:
                                published.append(note if not self.cfg.multi else f"{n}: {note}")
                if self.cfg.multi:
                    badge = " · ".join(f"{'✅' if good else '❌'} {n}" for n, good in results.items()) or "no project changed"
                else:
                    badge = "✅ tests pass" if verify_ok else "❌ tests failing"
                pushed = ("\n" + "\n".join(published)) if published else ""
                await self.notify(f"Round {rnd} done · {badge}{pushed}\n\n{_tail(reply, 1500)}",
                                  buttons=[[("📋 Status", "status"), ("📝 Changes", "changes"), ("⏸ Stop", "stop")]])

                if not ok:
                    await self.notify("⚠️ The agent hit an error. Build paused – check /log, then /build to retry.")
                    return
                if "NEEDS_INPUT:" in reply:
                    self.state.awaiting_input = True
                    self._save_state()
                    question = reply[reply.index("NEEDS_INPUT:") + len("NEEDS_INPUT:"):].strip()
                    await self.notify(f"❓ The PM needs your input:\n\n{question}\n\nReply here, or tap below – the build "
                                      "resumes after your answer.",
                                      buttons=[[("👍 Use your suggestions", "pm:defaults")]])
                    return
                if "PROJECT_COMPLETE:" in reply:
                    if verify_ok and os.environ.get("SKIP_E2E") == "1":
                        await self.notify("🧪 Running the full test suite, including end-to-end tests, before accepting completion…")
                        verify_ok = True
                        for n in scope:
                            good, _ = await self.verify(full=True, project=n)
                            verify_ok = verify_ok and good
                    if verify_ok:
                        await self.notify("🎉 " + ("Project complete and verified." if not self.cfg.multi
                                                   else f"All work in scope is complete and verified ({', '.join(scope)})."))
                        return
                    self.state.inbox.append("You reported PROJECT_COMPLETE but verify.sh fails. It is not complete.")
            await self.notify(f"⏹ Stopped after {self.cfg.max_rounds} rounds (MAX_BUILD_ROUNDS). Send /build to continue.")
        except asyncio.CancelledError:
            await self.notify("⏹ Build stopped.")
            raise
        finally:
            self._save_state()

    # ---------------------------------------------------------------- verify
    async def verify(self, full: bool = False, project: str | None = None) -> tuple[bool, str]:
        """Run a project's scripts/verify.sh (the main project by default).
        full=True forces the end-to-end tests even if SKIP_E2E=1."""
        name = project or self.main_name
        folder = self.cfg.workspace if name == self.main_name else self.project_root(name)[1]
        script = folder / "scripts" / "verify.sh"
        env = dict(os.environ)
        if full:
            env["SKIP_E2E"] = "0"
        if not script.exists():
            text, ok = (f"{name}: there is no scripts/verify.sh, so nothing proves this project works. "
                        "Create it (the same contract as the main project's: run every test suite, print a "
                        "'::: <suite>: ... OK/FAILED' line per suite, end with 'VERIFY: PASS' or 'VERIFY: FAIL', "
                        "exit non-zero on failure)."), False
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *sandbox.as_agent(["bash", str(script)]), cwd=folder, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=self.cfg.verify_timeout_s)
                text, ok = out.decode(errors="replace"), proc.returncode == 0
            except asyncio.TimeoutError:
                proc.kill()
                text, ok = "verify.sh timed out", False
        if name == self.main_name:
            self.state.last_verify = text
        if name in self.state.projects:
            self.state.projects[name]["last_verify"] = text[-6000:]
            self.state.projects[name]["verified_at"] = time.time()
        self._save_state()
        self._log(f"VERIFY {name} ok={ok}")
        return ok, _verify_summary(text)

    def _project_head(self, name: str) -> str:
        """HEAD sha + whether there are uncommitted changes: tells which projects a round touched."""
        folder = self.project_root(name)[1]
        if not (folder / ".git").exists():
            return ""
        head = sandbox.git("rev-parse", "HEAD", cwd=folder).stdout.strip()
        dirty = sandbox.git("status", "--porcelain", cwd=folder).stdout.strip()
        return f"{head}:{hash(dirty)}"

    def open_task_count(self, name: str) -> int:
        from status_view import parse_board
        board = self.project_root(name)[1] / "docs" / "PROGRESS.md"
        if not board.exists():
            return 0
        return sum(1 for t in parse_board(board.read_text(errors="replace")) if t.group in ("todo", "active"))

    # ------------------------------------------------------- git / GitHub / deploy
    async def _exec(self, *cmd: str, timeout: int = 300, cwd: Path | None = None,
                    token: str | None = None) -> tuple[int, str]:
        """Bot-privileged command (gh, git on the mirror). Never runs inside the agents' folder."""
        env = dict(os.environ)
        if token or self.cfg.github_token:
            env["GH_TOKEN"] = token or self.cfg.github_token
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd or self.cfg.data_dir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return proc.returncode, out.decode(errors="replace").strip()
        except FileNotFoundError:
            return 127, f"{cmd[0]} is not installed"
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"{' '.join(cmd[:3])} timed out"

    async def _agent_git(self, *args: str) -> tuple[int, str]:
        r = await asyncio.to_thread(sandbox.git, *args, cwd=self.cfg.workspace)
        return r.returncode, (r.stdout + r.stderr).strip()

    async def push(self) -> str:
        """Push the agents' committed main to GitHub, via the bot's mirror. One-line status."""
        if not self.cfg.github_enabled:
            return "GitHub isn't configured (GITHUB_REPO / GITHUB_TOKEN in .env)."
        code, sha = await self._agent_git("rev-parse", "HEAD")
        if code != 0:
            return "⚠️ Couldn't read the project's latest commit."
        if sha == self.state.last_pushed:
            return "⬆️ GitHub already up to date."
        m = str(self.mirror)
        code, out = await self._exec("git", "-C", m, "fetch", "-q", "--no-tags",
                                     f"file://{self.cfg.workspace.resolve()}", "+refs/heads/main:refs/heads/main")
        if code != 0:
            self._log(f"PUSH FAILED (mirror fetch) {out[-400:]!r}")
            return f"⚠️ Couldn't prepare the push.\n{_tail(out, 300)}"
        code, out = await self._exec("git", "-C", m, "push", "origin", "refs/heads/main:refs/heads/main", timeout=180)
        if code != 0:
            self._log(f"PUSH FAILED {out[-500:]!r}")
            hint = (" GitHub has commits the agents don't have (was something edited on GitHub?)."
                    if "rejected" in out or "fetch first" in out else "")
            return f"⚠️ Push to GitHub failed.{hint}\n{_tail(out, 400)}"
        self.state.last_pushed = sha
        self._save_state()
        self._log(f"PUSHED {sha[:12]}")
        self._watch(self._watch_pipeline(sha))
        return f"⬆️ Pushed to GitHub ({sha[:7]}). Tests + staging deploy running on GitHub."

    def _watch(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.watchers.add(task)
        task.add_done_callback(self.watchers.discard)

    async def _find_run(self, list_args: list[str], created_after: float | None = None,
                        wait_s: int = 600) -> dict | None:
        """Wait for a matching run to appear and return it (identified by its databaseId)."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            code, out = await self._exec(
                "gh", "run", "list", "-R", self.cfg.github_repo, "-L", "5",
                "--json", "databaseId,status,conclusion,url,headSha,createdAt,event", *list_args)
            if code == 127:
                return None
            if code == 0 and out.startswith("["):
                for run in json.loads(out):
                    if created_after is None or _iso_ts(run["createdAt"]) >= created_after:
                        return run
            await asyncio.sleep(self.cfg.pipeline_poll_s)
        return None

    async def _follow_run(self, run_id: int) -> dict | None:
        """Poll one exact run until it completes. Survives GitHub/API hiccups."""
        deadline = time.time() + self.cfg.pipeline_timeout_s
        errors, warned = 0, False
        while time.time() < deadline:
            code, out = await self._exec("gh", "run", "view", str(run_id), "-R", self.cfg.github_repo,
                                         "--json", "databaseId,status,conclusion,url,headSha")
            if code == 0 and out.startswith("{"):
                errors = 0
                run = json.loads(out)
                if run.get("status") == "completed":
                    return run
            else:
                errors += 1
                self._log(f"GH POLL ERROR run={run_id} {out[-200:]!r}")
                if errors >= 10 and not warned:
                    warned = True
                    await self.notify(f"⚠️ GitHub isn't answering about run {run_id} – I'll keep checking.")
            await asyncio.sleep(self.cfg.pipeline_poll_s * (1 if errors < 3 else 2))
        return None

    async def _ci_log_for_agents(self, run_id: int) -> str | None:
        """Agents have no GitHub access, so the bot fetches the failure log for them."""
        code, out = await self._exec("gh", "run", "view", str(run_id), "-R", self.cfg.github_repo,
                                     "--log-failed", timeout=120)
        if code != 0 or not out:
            return None
        d = self.cfg.workspace / ".ci-logs"
        d.mkdir(exist_ok=True)
        f = d / f"run-{run_id}.log"
        f.write_text("\n".join(out.splitlines()[-400:]) + "\n")
        gi = self.cfg.workspace / ".gitignore"
        if gi.exists() and ".ci-logs/" not in gi.read_text():
            gi.write_text(gi.read_text().rstrip("\n") + "\n\n# CI failure logs fetched by the bot\n.ci-logs/\n")
        sandbox.give_to_agent(d, gi)
        return str(f.relative_to(self.cfg.workspace))

    async def _watch_pipeline(self, sha: str) -> None:
        found = await self._find_run(["--workflow", "pipeline.yml", "--commit", sha])
        if not found:
            return
        run = found if found.get("status") == "completed" else await self._follow_run(found["databaseId"])
        if run is None:
            return
        if run["conclusion"] == "success":
            where = f"\nStaging: {self.cfg.staging_url}" if self.cfg.staging_url else ""
            await self.notify(f"🚀 GitHub pipeline passed for {sha[:7]}.{where}\n{run['url']}",
                              buttons=[[("🖼 Screens", "screens"), ("🚀 Deploy to production…", "deploy:preview")]])
        elif run["conclusion"] != "cancelled":   # cancelled = superseded by a newer push
            logf = await self._ci_log_for_agents(run["databaseId"])
            await self.notify(f"❌ GitHub pipeline failed for {sha[:7]} – the agents will fix it in the "
                              f"next build round (/build).\n{run['url']}")
            where = f"The failure log is in `{logf}`." if logf else "The failure log couldn't be fetched."
            self.state.inbox.append(
                f"The GitHub pipeline FAILED for commit {sha[:12]} (run {run['databaseId']}). Make fixing it "
                f"the first task, for devops-engineer. {where}")
            self._save_state()

    async def running_version(self, base_url: str | None) -> str | None:
        """Ask a live environment which version it runs (public /version.json)."""
        if not base_url:
            return None
        import urllib.request

        def get():
            try:
                req = urllib.request.Request(base_url.rstrip("/") + "/version.json",
                                             headers={"User-Agent": "agent-team-bot"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    return json.loads(r.read().decode())["version"]
            except Exception:
                return None
        return await asyncio.to_thread(get)

    async def deploy_preview(self) -> tuple[str, bool]:
        """What /deploy would put live, based on what staging really runs. (text, can_deploy)"""
        if not self.cfg.github_enabled:
            return "GitHub isn't configured yet, so there's nothing to deploy. See the README.", False
        staging = await self.running_version(self.cfg.staging_url)
        prod = await self.running_version(self.cfg.production_url)
        if not staging:
            return ("I can't see a version running on staging yet (no /version.json), so there's nothing to promote. "
                    "Staging gets a version after the first green build that includes the backend and frontend."), False
        code, out = await self._exec("git", "-C", str(self.mirror), "log", "-1", "--format=%s (%cr)", staging)
        what = out if code == 0 and out else "commit " + staging
        lines = [f"🚀 Production will get exactly what staging runs now:",
                 f"• version {staging[:7]} – {what}",
                 f"• production currently runs: {prod[:7] if prod else 'nothing yet'}"]
        if prod and prod == staging:
            lines.append("\nProduction already runs this version.")
        if self.cfg.staging_url:
            lines.append(f"\nTry it first: {self.cfg.staging_url}")
        lines.append("\nThe server backs up the database first and rolls back automatically if checks fail.")
        return "\n".join(lines), not (prod and prod == staging)

    async def deploy_drill(self) -> str:
        if not self.cfg.github_enabled:
            return "GitHub isn't configured yet."
        started = time.time() - 5
        code, out = await self._exec("gh", "workflow", "run", "deploy-drill.yml", "-R", self.cfg.github_repo)
        if code != 0:
            return f"⚠️ Couldn't start the fire drill:\n{_tail(out, 400)}"
        self._log("DEPLOY drill requested")
        self._watch(self._watch_production(started, "deploy-drill.yml", "Drill"))
        return ("🧯 Fire drill started on staging: a deliberately broken version is released, and the server must "
                "detect it and roll back by itself. Takes 2–4 minutes; staging is briefly unavailable.")

    async def deploy_history(self) -> str:
        if not self.cfg.github_enabled:
            return "GitHub isn't configured yet."
        lines = ["📜 Deployment history"]
        for wf, label in (("deploy-production.yml", "Production"), ("pipeline.yml", "Staging (via pipeline)"),
                          ("deploy-drill.yml", "Fire drills")):
            code, out = await self._exec("gh", "run", "list", "-R", self.cfg.github_repo, "--workflow", wf, "-L", "6",
                                         "--json", "createdAt,conclusion,status,headSha,displayTitle")
            runs = json.loads(out) if code == 0 and out.startswith("[") else []
            lines.append(f"\n{label}:")
            if not runs:
                lines.append("  (none)")
            icons = {"success": "✅", "failure": "❌", "cancelled": "⏹"}
            for r in runs:
                icon = icons.get(r.get("conclusion") or "", "⏳")
                when = r["createdAt"][:16].replace("T", " ")
                lines.append(f"  {icon} {when} UTC · {r['headSha'][:7]} · {_tail(r.get('displayTitle', ''), 38)}")
        return "\n".join(lines)

    async def deploy_production(self) -> str:
        if not self.cfg.github_enabled:
            return "GitHub isn't configured yet."
        started = time.time() - 5
        code, out = await self._exec("gh", "workflow", "run", "deploy-production.yml", "-R", self.cfg.github_repo)
        if code != 0:
            return f"⚠️ Couldn't start the production deploy:\n{_tail(out, 500)}"
        self._log("DEPLOY production requested")
        self._watch(self._watch_production(started))
        return "🚀 Production deploy started. I'll tell you when it's live (usually 1–3 minutes)."

    async def _watch_production(self, started: float, workflow: str = "deploy-production.yml",
                                label: str = "Production") -> None:
        await asyncio.sleep(5)
        found = await self._find_run(["--workflow", workflow], created_after=started, wait_s=180)
        run = None
        if found:
            run = found if found.get("status") == "completed" else await self._follow_run(found["databaseId"])
        if run is None:
            await self.notify(f"⚠️ Couldn't follow the {label.lower()} run. Check the Actions tab on GitHub.")
            return
        await self.notify(self._describe_deploy(run, label))

    def _describe_deploy(self, run: dict, label: str) -> str:
        if label == "Drill":
            return (("✅ Fire drill passed: the broken version was rejected and staging was rolled back "
                     "automatically.") if run["conclusion"] == "success" else
                    f"❌ Fire drill FAILED – rollback did not behave as expected. Check: {run['url']}")
        if run["conclusion"] == "success":
            where = f"\n{self.cfg.production_url}" if self.cfg.production_url else ""
            return f"✅ Production is live – it now runs the same version as staging.{where}"
        return ("❌ Production deploy failed. The deploy gate restores the previous version automatically "
                f"when checks fail; the run log says exactly what happened:\n{run['url']}")

    # ---------------------------------------------------------------- digest
    def digest_due(self) -> bool:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if not self.state.digest_enabled:
            return False
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        hh, mm = (int(x) for x in self.cfg.digest_time.split(":"))
        return (now.hour, now.minute) >= (hh, mm) and self.state.last_digest != now.date().isoformat()

    def mark_digest_sent(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        self.state.last_digest = datetime.now(ZoneInfo(self.cfg.timezone)).date().isoformat()
        self._save_state()

    # --------------------------------------------------------------- preview
    async def preview_start(self) -> str:
        """Run the app's dev servers (Django :8000, Angular :4200) until stopped."""
        if self.build_task and not self.build_task.done():
            return "A build is running and the agents use the app's ports for testing. Try /preview when it's idle."
        self._kill_previews()
        ws = self.cfg.workspace
        cmds = {}
        if (ws / "backend" / "manage.py").exists():
            cmds["backend"] = (ws / "backend",
                "pip install -q -r requirements.txt >/dev/null 2>&1; python manage.py migrate --noinput; "
                "exec python manage.py runserver 0.0.0.0:8000 --noreload")
        if (ws / "frontend" / "angular.json").exists():
            proxy = " --proxy-config proxy.conf.json" if (ws / "frontend" / "proxy.conf.json").exists() else ""
            cmds["frontend"] = (ws / "frontend",
                "[ -d node_modules ] || npm ci --no-audit --no-fund; "
                f"exec npx ng serve --host 0.0.0.0 --port 4200{proxy}")
        if not cmds:
            return "There's no app to preview yet – the agents haven't created the backend or frontend."
        for name, (cwd, cmd) in cmds.items():
            logf = open(self.cfg.data_dir / f"preview-{name}.log", "w")
            self.preview_procs[name] = await asyncio.create_subprocess_exec(
                *sandbox.as_agent(["bash", "-c", cmd]), cwd=cwd, stdout=logf,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        self._log(f"PREVIEW started {list(cmds)}")
        parts = " and ".join(cmds)
        return (f"▶️ Starting the {parts}. Give it about a minute, then on your laptop run:\n\n"
                "ssh -L 4200:localhost:4200 -L 8000:localhost:8000 root@YOUR_SERVER_IP\n\n"
                "and open http://localhost:4200 (Django admin: http://localhost:8000/admin/).\n"
                "/preview log shows the server output, /preview stop stops it.")

    def _kill_previews(self) -> bool:
        import signal
        killed = False
        for proc in self.preview_procs.values():
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    killed = True
                except ProcessLookupError:
                    pass
        self.preview_procs.clear()
        return killed

    def preview_stop(self) -> str:
        return "⏹ Preview stopped." if self._kill_previews() else "No preview is running."

    def preview_log(self, n: int = 15) -> str:
        out = []
        for name in ("backend", "frontend"):
            f = self.cfg.data_dir / f"preview-{name}.log"
            if f.exists():
                proc = self.preview_procs.get(name)
                state = "running" if proc and proc.returncode is None else "stopped"
                lines = f.read_text(errors="replace").splitlines()[-n:]
                out.append(f"── {name} ({state}) ──\n" + "\n".join(lines))
        return "\n\n".join(out) or "No preview has been started yet. Send /preview."

    # ---------------------------------------------------------------- status
    def status(self, full: bool = False, project: str | None = None) -> str:
        """Telegram HTML status for one project (the active one by default): mode, tests, tasks by
        state, plan progress, links, plus one line per other project."""
        import html as _h
        from status_view import parse_board, render
        name = project or self.state.active_project or self.main_name
        if name not in self.state.projects and name != self.main_name:
            name = self.main_name
        running = bool(self.build_task and not self.build_task.done())
        scope = self.state.build_scope or [self.main_name]
        if running:
            mode = "🔨 <b>Building</b>" + (f" · round {self.state.rounds_done}" if self.state.rounds_done else "")
            if self.cfg.multi:
                mode += " · " + ", ".join(_h.escape(n) for n in scope)
        elif self.state.awaiting_input:
            mode = "❓ <b>Waiting for your answer</b>"
        elif self.ops.label:
            mode = f"💬 <b>{_h.escape(self.ops.label[0].upper() + self.ops.label[1:])}</b>"
        else:
            mode = "💤 <b>Idle</b>"
        job = self.studio.active_job() if getattr(self, "studio", None) else None
        if job:
            mode += f"  ·  🎛 studio job: {_h.escape(job['agent'])} ({job['status']})"

        info = self.project_info(name)
        lv = self.state.last_verify if name == self.main_name else (info.get("last_verify") or "not run yet")
        if lv == "not run yet":
            tests = "🧪 Tests: not run yet"
        elif "VERIFY: PASS" in lv:
            tests = "🧪 Tests: ✅ passing"
        elif "no scripts/verify.sh" in lv:
            tests = "🧪 Tests: ⚠️ no scripts/verify.sh yet"
        else:
            failed = [l.split("::: ", 1)[1].replace(" FAILED", "") for l in lv.splitlines() if l.endswith("FAILED")]
            tests = "🧪 Tests: ❌ failing" + (f" ({_h.escape(', '.join(failed[:3]))})" if failed else "")

        read = lambda p: p.read_text(errors="replace") if p.exists() else None
        title, root = self.project_root(name)
        extra = []
        repo = info.get("repo") if self.cfg.multi or name != self.main_name else (self.cfg.github_repo if self.cfg.github_enabled else None)
        if repo:
            how = "push to main → staging" if info.get("mode") == "main" else "feature branches → pull requests"
            extra.append(f"🐙 GitHub: <code>{_h.escape(repo)}</code> ({how})")
        elif name != self.main_name:
            extra.append(f"🐙 GitHub: not linked (/project link {_h.escape(name)} owner/repo)")
        if name == self.main_name and self.cfg.staging_url:
            extra.append(f"🌐 Staging: {_h.escape(self.cfg.staging_url)}")
        if self.state.curator_pending:
            extra.append("🧠 A playbook proposal is waiting: /learn diff")
        if self.state.inbox:
            extra.append(f"📥 {len(self.state.inbox)} message(s) queued for the PM")
        for other in self.project_names():
            if other == name:
                continue
            o_title, o_root = self.project_root(other)
            b = o_root / "docs" / "PROGRESS.md"
            tasks = parse_board(b.read_text(errors="replace")) if b.exists() else []
            done = sum(1 for t in tasks if t.group == "done")
            active = sum(1 for t in tasks if t.group == "active")
            summary = f"{done}/{len(tasks)} done" + (f", {active} in progress" if active else "") if tasks else "no plan yet"
            extra.append(f"📁 {_h.escape(o_title)}: {summary} (/status {_h.escape(other)})")
        board, spec = read(root / "docs" / "PROGRESS.md"), read(root / "docs" / "SPEC.md")
        if not board:
            extra.insert(0, "⚠️ No plan yet for this project (docs/PROGRESS.md) – ask the PM to write one.")
        return render(title, mode, tests, board, spec, extra, full=full)

    def recent_log(self, n: int = 25) -> str:
        if not self.log_file.exists():
            return "(log is empty)"
        return "\n".join(self.log_file.read_text().splitlines()[-n:])

    def reset_session(self) -> None:
        self.state.session_id = None
        self.state.awaiting_input = False
        self.state.inbox.clear()
        self._save_state()


def _iso_ts(s: str) -> float:
    from datetime import datetime
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "…" + text[-n:]


def _verify_summary(text: str) -> str:
    lines = [l for l in text.splitlines() if l.startswith(":::") and (" OK" in l or "FAILED" in l or "skipped" in l)]
    final = [l for l in text.splitlines() if l.startswith("VERIFY:")]
    return "\n".join(l.replace("::: ", "• ") for l in lines + final) or text[-500:]
