"""Multi-project layout: a master folder for the team, one folder (and git repo) per project.

    /workspace                    master: PM's working folder, team infra, its own local git repo
      CLAUDE.md                   team rules (master-template/CLAUDE.md)
      .claude/agents, skills      the team (shared by every project)
      docs/LESSONS.md, STANDARDS  lessons and standards for all projects
      projects/<name>/            one project = one git repo (its own docs/, scripts/verify.sh)

Old installs had the main project's repo AT /workspace. `migrate_to_master` moves it (and any other
project folders that were nested inside it) into projects/ once, keeping history and state.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

log = logging.getLogger("layout")

# Shared by every project: they live in the master folder.
TEAM_PATHS = (".claude/agents", ".claude/skills", "docs/LESSONS.md", "docs/STANDARDS.md",
              "design/TRENDS.md", "design/REFERENCES.md")
MASTER_GITIGNORE = "/projects/\n.ci-logs/\n__pycache__/\n"


def migrate_to_master(o: "Orchestrator") -> bool:
    """Old layout (main repo at the master folder) -> master + projects/. Returns True if it moved."""
    master, main = o.cfg.master, o.cfg.workspace
    if not o.cfg.multi or not (master / ".git").exists() or main.exists():
        return False
    if (master / "projects").exists() and not (master / "projects").is_dir():
        return False
    log.warning("Moving to the multi-project layout: %s -> %s", master, main)
    nested = {info["path"].strip("/"): name for name, info in o.state.projects.items()
              if info.get("path") and "/" not in info["path"].strip("/") and info["path"].strip("/") != "."}
    main.mkdir(parents=True)
    moved_projects = []
    for entry in sorted(master.iterdir()):
        if entry.name == "projects":
            continue
        if entry.name in nested and entry.is_dir():
            name = nested[entry.name]
            dest = master / "projects" / name
            shutil.move(str(entry), str(dest))
            o.state.projects[name]["path"] = f"projects/{name}"
            moved_projects.append(name)
        else:
            shutil.move(str(entry), str(main / entry.name))
    # The knowledge curator now works on the master's repo: drop its old worktree and any pending
    # proposal made against the main project's repo (it will propose again on its next run).
    wt = o.cfg.curator_worktree or (sandbox.agent_home() / "curator-worktree" if sandbox.separated()
                                    else o.cfg.data_dir / "curator-worktree")
    shutil.rmtree(wt, ignore_errors=True)
    sandbox.git("worktree", "prune", cwd=main)
    o.state.curator_pending = None
    o.state.inbox.append(
        "The workspace is now multi-project. You work from the master folder (/workspace): the team "
        "(.claude/agents, .claude/skills), docs/LESSONS.md and docs/STANDARDS.md live there; every project has "
        f"its own folder and git repo under projects/ ({', '.join([o.main_name] + moved_projects)}). Each project "
        "keeps its own docs/SPEC.md, docs/PROGRESS.md and scripts/verify.sh. Paths you knew as docs/... are now "
        f"projects/<name>/docs/... (the main project is projects/{o.main_name}).")
    o._save_state()
    o._log(f"LAYOUT migrated to master + projects/ ({o.main_name}, {', '.join(moved_projects) or 'no others'})")
    return True


def init_master(o: "Orchestrator") -> None:
    """Create the master folder's team files (once), copied from the main project so lessons and
    approved playbook improvements carry over; then keep it a local git repo for the curator."""
    if not o.cfg.multi:
        return
    master, main, tpl = o.cfg.master, o.cfg.workspace, o.cfg.template_dir
    master_tpl = Path(__file__).resolve().parent.parent / "master-template"
    (master / "projects").mkdir(parents=True, exist_ok=True)
    added = []
    for rel in TEAM_PATHS:
        dest = master / rel
        if dest.exists():
            continue
        src = main / rel if (main / rel).exists() else tpl / rel
        if not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        added.append(rel)
    # Template files the team gained later (new agents/playbooks) are added, never overwritten.
    for rel in TEAM_PATHS:
        src = tpl / rel
        if src.is_dir():
            for f in sorted(p for p in src.rglob("*") if p.is_file()):
                dest = master / f.relative_to(tpl)
                if not dest.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, dest)
                    added.append(str(f.relative_to(tpl)))
    if not (master / "CLAUDE.md").exists() and (master_tpl / "CLAUDE.md").exists():
        shutil.copy2(master_tpl / "CLAUDE.md", master / "CLAUDE.md")
        added.append("CLAUDE.md")
    gi = master / ".gitignore"
    if not gi.exists():
        gi.write_text(MASTER_GITIGNORE)
        added.append(".gitignore")
    sandbox.give_to_agent(master)
    if not (master / ".git").exists():
        sandbox.git("init", "-q", "-b", "main", cwd=master, check=True)
    if added:
        sandbox.git("add", "-A", cwd=master)
        sandbox.git("commit", "-q", "-m", "Team setup: shared agents, playbooks, lessons", cwd=master)
