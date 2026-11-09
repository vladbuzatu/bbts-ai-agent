"""Owner conveniences: /today (+ evening digest), /changes, /screens, /server, /undo, /ask,
and the "now doing" line for the live status. Read-only helpers use git as the agent
user (sandbox.git); /undo and /ask go through the operation controller."""
from __future__ import annotations

import html
import os
import re
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

TASK_RE = re.compile(r"^(T-\d+)[:\s]")
e = html.escape


def _project(o: "Orchestrator") -> Path:
    """The project the owner is looking at (/project, or the one the PM switched to)."""
    return o.project_root()[1]


def _git(o: "Orchestrator", *args: str) -> str:
    r = sandbox.git(*args, cwd=_project(o))
    return r.stdout if r.returncode == 0 else ""


def _now(o: "Orchestrator") -> datetime:
    return datetime.now(ZoneInfo(o.cfg.timezone))


# ------------------------------------------------------------------ live status
def current_activity(o: "Orchestrator") -> str | None:
    """Turn the newest agent log line into a short human description."""
    if not o.log_file.exists():
        return None
    try:
        lines = o.log_file.read_text(errors="replace").splitlines()[-40:]
    except OSError:
        return None
    for line in reversed(lines):
        body = line[20:]
        if body.startswith("TOOL "):
            parts = body.split(" ", 2)
            tool, detail = parts[1], (parts[2] if len(parts) > 2 else "")
            detail = detail.replace(str(o.cfg.master) + "/", "")
            if tool in ("Agent", "Task"):
                return f"🤖 {detail or 'a specialist'} is working"
            if tool == "Bash":
                return f"⚙️ running: {detail[:60]}"
            if tool in ("Write", "Edit", "MultiEdit"):
                return f"✏️ editing {Path(detail).name or detail}"
            if tool in ("Read", "Grep", "Glob"):
                return "🔎 reading the project"
            if "screenshot" in tool:
                return "📸 sending screenshots"
            return f"🛠 {tool}"
        if body.startswith("VERIFY"):
            return "🧪 running the full test suite"
        if body.startswith("PUSHED"):
            return "⬆️ pushed to GitHub"
    return None


# ------------------------------------------------------------------- /today
def today(o: "Orchestrator") -> tuple[str, bool]:
    """HTML summary of today. Returns (text, anything_happened)."""
    now = _now(o)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    log = _git(o, "log", f"--since={midnight.isoformat()}", "--format=%s")
    subjects = [s for s in log.splitlines() if s.strip()]
    tasks = sorted({m.group(1) for s in subjects if (m := TASK_RE.match(s))}, key=lambda t: int(t[2:]))
    m = o.state.metrics.get(now.date().isoformat(), {"rounds": 0, "red": 0})
    from status_view import parse_board
    board_md = (_project(o) / "docs/PROGRESS.md")
    board = parse_board(board_md.read_text(errors="replace")) if board_md.exists() else []
    titles = {t.id: t.title for t in board}
    done, total = sum(t.group == "done" for t in board), len(board)
    pushes = deploys = 0
    if o.log_file.exists():
        stamp = midnight.strftime("%Y-%m-%d")
        for line in o.log_file.read_text(errors="replace").splitlines():
            if line.startswith(stamp):
                pushes += " PUSHED " in line
                deploys += " DEPLOY " in line
    jobs = [j for j in o.studio.st["jobs"].values()
            if j.get("status") == "done" and j.get("created", 0) >= midnight.timestamp()] if o.studio else []

    lines = [f"<b>☀️ Today · {now:%a %d %b}</b>", ""]
    if tasks:
        lines.append(f"✅ <b>Finished {len(tasks)} task(s)</b>")
        lines += [f"  • <code>{e(t)}</code> {e(titles.get(t, ''))}" for t in tasks[:10]]
        lines.append("")
    if m["rounds"]:
        green = m["rounds"] - m["red"]
        lines.append(f"🔁 {m['rounds']} build round(s): {green} green, {m['red']} with failing tests")
    lines.append(f"📝 {len(subjects)} commit(s)" + (f" · ⬆️ {pushes} push(es)" if pushes else "")
                 + (f" · 🚀 {deploys} production deploy(s)" if deploys else ""))
    if jobs:
        lines.append(f"🎛 Studio: {len(jobs)} job(s) delivered – " + ", ".join(e(j['agent']) for j in jobs[:3]))
    if total:
        lines.append(f"📋 Overall: {done}/{total} tasks done")
    waiting = []
    if o.state.awaiting_input:
        waiting.append("the PM is waiting for your answer")
    if o.state.curator_pending:
        waiting.append("a playbook proposal is waiting (/learn diff)")
    if waiting:
        lines += ["", "👉 <b>Needs you:</b> " + "; ".join(waiting)]
    happened = bool(subjects or m["rounds"] or jobs)
    if not happened:
        lines = [f"<b>☀️ Today · {now:%a %d %b}</b>", "", "Nothing happened yet today."]
    return "\n".join(lines), happened


# ------------------------------------------------------------------ /changes
GROUPS = (("✅ Tasks", lambda s: bool(TASK_RE.match(s))),
          ("🧠 Playbooks & lessons", lambda s: s.startswith(("Approve curator", "Curator", "Link agents"))
           or "lesson" in s.lower()),
          ("↩️ Undone", lambda s: s.startswith("Revert")),
          ("🔧 Other", lambda s: True))


def changes(o: "Orchestrator") -> str:
    since = o.state.changes_seen
    rng = f"{since}..HEAD" if since and _git(o, "cat-file", "-t", since).strip() == "commit" else None
    args = ["log", "--format=%h\t%s\t%cr"] + ([rng] if rng else ["--since=24 hours ago"])
    rows = [l.split("\t") for l in _git(o, *args).splitlines() if l.count("\t") == 2]
    head = _git(o, "rev-parse", "HEAD").strip()
    if head:
        o.state.changes_seen = head
        o._save_state()
    if not rows:
        return "No changes since you last checked." if rng else "No changes in the last 24 hours."
    stat = _git(o, "diff", "--shortstat", f"{rows[-1][0]}~1", "HEAD").strip() if len(rows) else ""
    out = [f"<b>📝 What changed {'since you last checked' if rng else 'in the last 24 hours'}</b>",
           f"<i>{len(rows)} commit(s){' · ' + e(stat) if stat else ''}</i>", ""]
    used = set()
    for title, pred in GROUPS:
        items = [r for r in rows if r[0] not in used and pred(r[1])]
        if not items:
            continue
        used |= {r[0] for r in items}
        out.append(f"<b>{title}</b>")
        out += [f"  • {e(r[1][:70])} <i>· {e(r[2])}</i>" for r in items[:12]]
        if len(items) > 12:
            out.append(f"  <i>… +{len(items) - 12} more</i>")
        out.append("")
    return "\n".join(out).strip()


# ------------------------------------------------------------------- /screens
def screenshots(o: "Orchestrator", which: str = "") -> list[Path]:
    ws = _project(o).resolve()
    folders = {"mockups": [ws / "design/mockups"], "app": [ws / "design/screens"]}.get(
        which, [ws / "design/screens", ws / "design/mockups"])
    files = []
    for d in folders:
        if d.exists():
            for p in d.rglob("*.png"):
                r = p.resolve()
                if ws in r.parents and r.is_file():   # never follow links out of the project
                    files.append(r)
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:10]


# -------------------------------------------------------------------- /server
def _meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return out


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f} GB"


def _bar(frac: float, width: int = 10) -> str:
    frac = max(0.0, min(1.0, frac))
    return "▓" * round(frac * width) + "░" * (width - round(frac * width))


async def server(o: "Orchestrator") -> str:
    out = ["<b>🖥 Server</b>", ""]
    try:
        load = os.getloadavg()
        cpus = os.cpu_count() or 1
        out.append(f"CPU   {_bar(load[0] / cpus)}  load {load[0]:.1f} on {cpus} core(s)")
    except OSError:
        pass
    mi = _meminfo()
    if mi.get("MemTotal"):
        used = mi["MemTotal"] - mi.get("MemAvailable", 0)
        out.append(f"RAM   {_bar(used / mi['MemTotal'])}  {_gb(used)} / {_gb(mi['MemTotal'])}")
        if mi.get("SwapTotal"):
            su = mi["SwapTotal"] - mi.get("SwapFree", 0)
            out.append(f"Swap  {_bar(su / mi['SwapTotal'])}  {_gb(su)} / {_gb(mi['SwapTotal'])}")
    du = shutil.disk_usage(o.cfg.workspace if o.cfg.workspace.exists() else "/")
    out.append(f"Disk  {_bar(du.used / du.total)}  {_gb(du.used)} / {_gb(du.total)}")
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        out.append(f"Up    {int(up // 86400)}d {int(up % 86400 // 3600)}h")
    except OSError:
        pass

    out += ["", "<b>🤖 Claude usage</b>"]
    usage = o.state.usage or {}
    names = {"five_hour": "5-hour window", "seven_day": "weekly", "seven_day_opus": "weekly (Opus)",
             "seven_day_sonnet": "weekly (Sonnet)", "overage": "extra usage"}
    if not usage:
        out.append("  <i>no data yet – shown after the next agent run</i>")
    for kind, u in usage.items():
        util = u.get("utilization")
        reset = u.get("resets_at")
        when = datetime.fromtimestamp(reset, ZoneInfo(o.cfg.timezone)).strftime("%a %H:%M") if reset else "?"
        pct = f"{util * 100:.0f}% used" if isinstance(util, (int, float)) else u.get("status", "")
        bar = _bar(util) + "  " if isinstance(util, (int, float)) else ""
        out.append(f"  {names.get(kind, kind)}: {bar}{pct} · resets {when}")

    out += ["", "<b>🧩 Activity</b>"]
    busy = o.ops.label
    out.append(f"  {'⏳ ' + e(busy) if busy else '💤 nothing exclusive running'}")
    if any(p.returncode is None for p in o.preview_procs.values()):
        out.append("  👀 app preview running (/preview stop)")
    s_ver = await o.running_version(o.cfg.staging_url)
    p_ver = await o.running_version(o.cfg.production_url)
    if o.cfg.staging_url:
        out.append(f"  🌐 staging runs {s_ver[:7] if s_ver else '–'} · production runs {p_ver[:7] if p_ver else '–'}")
    if o.state.last_pushed:
        out.append(f"  ⬆️ last push {o.state.last_pushed[:7]}")
    import hostctl
    out += ["", "<b>🛠 Maintenance</b>"] + [f"  {e(line)}" for line in hostctl.summary_lines()]
    return "\n".join(out)


# ---------------------------------------------------------------------- /undo
def undo_candidate(o: "Orchestrator") -> dict | None:
    """The newest task commit (T-xx: …) that hasn't been reverted yet."""
    log = _git(o, "log", "-n", "60", "--format=%H\t%s\t%cr\t%b%x1e")
    reverted = set(re.findall(r"This reverts commit ([0-9a-f]{40})", log))
    for rec in log.split("\x1e"):
        parts = rec.strip().split("\t", 3)
        if len(parts) < 3:
            continue
        sha, subj, when = parts[0], parts[1], parts[2]
        if subj.startswith("Revert") or sha in reverted:
            continue
        if TASK_RE.match(subj):
            stat = _git(o, "show", "--shortstat", "--format=", sha).strip()
            return {"sha": sha, "subject": subj, "when": when, "stat": stat}
    return None


def undo(o: "Orchestrator", sha: str) -> str:
    busy = o.ops.busy()
    if busy:
        return busy
    if o.build_task and not o.build_task.done():
        return "A build is running. /stop it first, then /undo."
    cand = undo_candidate(o)
    if not cand or cand["sha"] != sha:
        return "That task is no longer the latest one to undo. Send /undo again to see the current one."
    r = sandbox.git("revert", "--no-edit", sha, cwd=_project(o))
    if r.returncode != 0:
        sandbox.git("revert", "--abort", cwd=_project(o))
        return ("⚠️ Couldn't undo it cleanly – later work depends on it. Ask the PM to remove it instead "
                f"(e.g. \"undo {TASK_RE.match(cand['subject']).group(1)} without breaking later tasks\").")
    task = TASK_RE.match(cand["subject"]).group(1)
    o.state.inbox.append(f"The owner undid {task} (git revert of {sha[:12]}). Set {task} back to `todo` on the "
                         "board and don't redo it unless the owner asks.")
    o._save_state()
    o._log(f"UNDO {sha[:12]} {cand['subject'][:60]}")
    return (f"↩️ Undid {e(cand['subject'])}. It reaches GitHub/staging with the next green round "
            "(or /push). The PM has been told.")


# ----------------------------------------------------------------------- /ask
ASK_MODE = """

# Mode: a direct question from the owner
The owner is asking you a question on Telegram. This is READ-ONLY: look at the project
files, but change nothing. Answer in at most 12 short lines, in plain language, citing
file paths where useful. If you don't know, say so."""


def team_agents(o: "Orchestrator") -> dict[str, dict]:
    from studio import parse_agent_file
    d = o.cfg.master / ".claude" / "agents"
    out = {}
    for f in sorted(d.glob("*.md")) if d.exists() else []:
        a = parse_agent_file(f)
        if a:
            out[a["name"]] = a
    return out


async def ask(o: "Orchestrator", name: str, question: str) -> str:
    agents = team_agents(o)
    if name not in agents:
        return "Ask one of: " + ", ".join(sorted(agents)) + "\nExample: /ask backend-dev how is the CSV import validated?"
    if not question.strip():
        return f"What do you want to ask {name}?"
    busy = o.ops.busy()
    if busy:
        return busy
    a = agents[name]
    options = ClaudeAgentOptions(
        max_buffer_size=sandbox.SDK_MAX_BUFFER,   # images read by agents can exceed the 1 MB default
        system_prompt={"type": "preset", "preset": "claude_code", "append": a["prompt"] + ASK_MODE},
        cwd=str(o.cfg.master),
        setting_sources=["project"],
        tools=["Read", "Grep", "Glob"],          # read-only, enforced by the tool set
        disallowed_tools=["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Task"],
        permission_mode="bypassPermissions",
        user=sandbox.agent_user(),
        env=sandbox.agent_env(),
        max_turns=15,
        model=a.get("model") or o.cfg.model,
    )
    final, last = "", ""
    async with o.ops.run(f"a question for {name}"):
        o._log(f"ASK {name}: {question[:120]!r}")
        try:
            async for msg in query(prompt=question, options=options):
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            last = b.text
                elif isinstance(msg, ResultMessage):
                    final = msg.result or last
        except Exception as ex:  # noqa: BLE001
            return f"⚠️ {name} couldn't answer: {ex}"
    return f"💬 {name}:\n\n{(final or last or '(no answer)').strip()}"
