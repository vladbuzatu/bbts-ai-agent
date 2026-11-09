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


def _markers(o: "Orchestrator") -> tuple[Path, Path]:
    return o.cfg.data_dir / "layout-migrated", o.cfg.data_dir / "layout-migrating.json"


def migrate_to_master(o: "Orchestrator") -> bool:
    """Old layout (main repo AT the master folder) -> master + projects/. Returns True if it moved.

    Runs at most once (a marker in the bot's data folder), and is crash-safe: the plan is written first,
    the main project is assembled in projects/.migrating-<name> and renamed into place at the very end,
    and an interrupted run is resumed on the next start."""
    import json
    if not o.cfg.multi:
        return False
    master, main = o.cfg.master, o.cfg.workspace
    done, plan_file = _markers(o)
    if done.exists():
        return False
    if plan_file.exists():
        plan = json.loads(plan_file.read_text())
        log.warning("Resuming an interrupted move to the multi-project layout")
    else:
        old_layout = (master / ".git").exists() and not (master / "projects").exists() and not main.exists()
        if not old_layout:
            done.write_text("not needed\n")
            return False
        nested = {}
        for name, info in o.state.projects.items():
            rel = (info.get("path") or "").strip("/")
            if rel and rel != "." and not info.get("main"):
                nested[name] = rel
        plan = {"main": main.name, "nested": nested}
        plan_file.write_text(json.dumps(plan))       # from here on, a crash resumes this exact plan
    log.warning("Moving to the multi-project layout: %s -> %s", master, main)
    staging = master / "projects" / f".migrating-{plan['main']}"
    staging.mkdir(parents=True, exist_ok=True)
    tops = {rel.split("/")[0] for rel in plan["nested"].values() if "/" not in rel}
    moved_projects = []
    # 1. other projects that lived directly in the old repo's folder -> projects/<name>
    for name, rel in plan["nested"].items():
        src, dest = master / rel, master / "projects" / name
        if "/" in rel:
            continue                                  # deeper paths travel with the main project (below)
        if src.is_dir() and not dest.exists():
            shutil.move(str(src), str(dest))
        if dest.exists():
            o.state.projects.setdefault(name, {})["path"] = f"projects/{name}"
            moved_projects.append(name)
    # 2. everything else -> the main project (staging folder)
    for entry in sorted(master.iterdir()):
        if entry.name == "projects" or entry.name in tops:
            continue
        shutil.move(str(entry), str(staging / entry.name))
    for name, rel in plan["nested"].items():           # nested deeper: now inside the main project
        if "/" in rel:
            o.state.projects.setdefault(name, {})["path"] = f"projects/{plan['main']}/{rel}"
    # 3. the main project goes live in one rename
    if not main.exists():
        staging.rename(main)
    o._save_state()
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
        f"its own folder and git repo under projects/ ({', '.join([plan['main']] + moved_projects)}). Each project "
        "keeps its own docs/SPEC.md, docs/PROGRESS.md and scripts/verify.sh. Paths you knew as docs/... are now "
        f"projects/<name>/docs/... (the main project is projects/{plan['main']}).")
    o._save_state()
    done.write_text("done\n")
    plan_file.unlink(missing_ok=True)
    o._log(f"LAYOUT migrated to master + projects/ ({plan['main']}, {', '.join(moved_projects) or 'no others'})")
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
