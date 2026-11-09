"""Scorecard per project: how well and how expensively the team works.

Recorded as the team works (state.score = {day: {project: {...counters}}}):
  rounds, red (rounds whose tests failed), turns, tokens, api_usd (what the turns would cost on the API;
  on a subscription it's a measure of usage, not a bill), minutes (agent time)
Derived at report time: tasks committed (git log, "<ID>: ..." commits), reviews requested, pull requests
opened/merged, deploys (main project), lab spend.
"""
from __future__ import annotations

import html
import os
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

TASK_COMMIT = re.compile(r"^[A-Z]{1,6}-\d+[:\s]")
KEEP_DAYS = 90


def _day(o: "Orchestrator", ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), ZoneInfo(o.cfg.timezone)).date().isoformat()


def _bucket(o: "Orchestrator", project: str) -> dict:
    day = o.state.score.setdefault(_day(o), {})
    for old in sorted(o.state.score)[:-KEEP_DAYS]:
        del o.state.score[old]
    return day.setdefault(project, {})


def add(o: "Orchestrator", project: str, **counters: float) -> None:
    b = _bucket(o, project)
    for k, v in counters.items():
        b[k] = round(b.get(k, 0) + v, 4)


def record_turn(o: "Orchestrator", projects: list[str], msg) -> None:
    """Split one agent turn's usage over the projects it worked on."""
    projects = projects or ["(other)"]
    usage = getattr(msg, "usage", None) or {}
    tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))
    share = 1 / len(projects)
    for p in projects:
        add(o, p, turns=share, tokens=tokens * share, api_usd=float(getattr(msg, "total_cost_usd", 0) or 0) * share,
            minutes=(getattr(msg, "duration_ms", 0) or 0) / 60000 * share)
    o._save_state()


def _sum(o: "Orchestrator", project: str, days: int) -> dict:
    since = (datetime.now(ZoneInfo(o.cfg.timezone)).date() - timedelta(days=days - 1)).isoformat()
    total: dict = {}
    for day, projects in o.state.score.items():
        if day >= since:
            for k, v in projects.get(project, {}).items():
                total[k] = total.get(k, 0) + v
    return total


def _git_tasks(o: "Orchestrator", project: str, days: int) -> int:
    folder = o.project_root(project)[1]
    if not (folder / ".git").exists():
        return 0
    out = sandbox.git("log", f"--since={days} days ago", "--format=%s", cwd=folder).stdout
    return len({line.split(":")[0].split()[0] for line in out.splitlines() if TASK_COMMIT.match(line)})


def _reviews(o: "Orchestrator", days: int) -> int:
    if not o.log_file.exists():
        return 0
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    return sum(1 for line in o.log_file.read_text(errors="replace").splitlines()
               if line[:10] >= since and "TOOL Agent code-reviewer" in line)


def report(o: "Orchestrator", days: int = 7) -> str:
    e = html.escape
    out = [f"<b>📊 Scorecard · last {days} days</b>"]
    total_usd = 0.0
    for name in o.project_names() + [n for n in ("studio",) if any("studio" in d for d in o.state.score.values())]:
        s = _sum(o, name, days)
        title = o.project_root(name)[0] if name in o.project_names() else "Studio jobs"
        rounds, red = int(s.get("rounds", 0)), int(s.get("red", 0))
        tasks = _git_tasks(o, name, days) if name in o.project_names() else 0
        prs = [p for p in o.state.prs.values() if p.get("project") == name
               and p.get("updated", 0) >= time.time() - days * 86400]
        merged = sum(1 for p in prs if p.get("state") == "merged")
        lab = 0.0
        if name == os.environ.get("LAB_PROJECT", "carstage"):
            lab = float(o.state.pm_lab.get("lab_spent") or 0) + sum(
                float(j.get("lab_spent") or 0) for j in o.state.studio.get("jobs", {}).values()
                if j.get("agent") == "model-lab" and j.get("created", 0) >= time.time() - days * 86400)
        if not (rounds or tasks or s.get("turns") or prs or lab):
            out.append(f"\n<b>{e(title)}</b>: no activity")
            continue
        green = f"{100 * (rounds - red) / rounds:.0f}% green" if rounds else "–"
        total_usd += s.get("api_usd", 0)
        out.append(f"\n<b>{e(title)}</b>")
        out.append(f"  ✅ {tasks} task(s) committed · {rounds} build round(s), {green}")
        if prs:
            out.append(f"  🔀 {len(prs)} pull request(s), {merged} merged")
        out.append(f"  🤖 {s.get('turns', 0):.0f} agent turns · {s.get('minutes', 0):.0f} min · "
                   f"{s.get('tokens', 0) / 1e6:.1f}M tokens (≈${s.get('api_usd', 0):.2f} at API prices)")
        if lab:
            out.append(f"  🧪 lab spend ≈${lab:.2f}")
    reviews = _reviews(o, days)
    if reviews:
        out.append(f"\n🔍 {reviews} code review(s) requested")
    out.append(f"\nUsage at API prices (all projects): ≈${total_usd:.2f}. On your subscription this is a "
               "measure of how much you use, not a bill.")
    return "\n".join(out)
