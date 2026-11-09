"""Readable /status for Telegram (HTML): what's done, what's in progress, what's left,
and how far each feature from the plan (docs/SPEC.md) has got."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

DONE = {"done", "complete", "completed", "✅"}
ACTIVE = {"in-progress", "in progress", "doing", "wip", "review", "in review", "🔄"}
BLOCKED = {"blocked", "⛔"}


@dataclass
class Task:
    id: str
    title: str
    owner: str
    status: str
    group: str  # done | active | todo | blocked


def _norm(s: str) -> str:
    return re.sub(r"[`*_]", "", s).strip().lower()


def parse_board(md: str) -> list[Task]:
    """Read the task table in PROGRESS.md, finding columns by their header names."""
    tasks: list[Task] = []
    header: list[str] | None = None
    for line in md.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            header = None if not tasks and header is None else header
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        low = [_norm(c) for c in cells]
        if "status" in low and ("task" in low or "id" in low):
            header = low
            continue
        if not header:
            continue
        get = lambda name, default="": cells[header.index(name)] if name in header and header.index(name) < len(cells) else default
        status = _norm(get("status"))
        if not status:
            continue
        group = ("done" if status in DONE else "active" if status in ACTIVE
                 else "blocked" if status in BLOCKED else "todo")
        tasks.append(Task(id=get("id"), title=re.sub(r"[`*]", "", get("task") or get("title")),
                          owner=_norm(get("owner")), status=status, group=group))
    return tasks


def parse_features(spec: str) -> list[tuple[str, int, int]]:
    """Features in SPEC.md: '### F1. Name' with '- [x]' / '- [ ]' acceptance criteria."""
    feats: list[list] = []
    for line in spec.splitlines():
        m = re.match(r"#{2,4}\s+(F\d+[a-z]?)[.:)]?\s+(.*)", line.strip())
        if m:
            name = m.group(2).strip()
            if "<feature name>" in name.lower():
                continue
            feats.append([f"{m.group(1)} {name}", 0, 0])
            continue
        c = re.match(r"\s*[-*]\s+\[( |x|X)\]", line)
        if c and feats:
            feats[-1][2] += 1
            if c.group(1).lower() == "x":
                feats[-1][1] += 1
    return [(n, d, t) for n, d, t in feats]


def _bar(done: int, total: int, width: int = 12) -> str:
    if total <= 0:
        return "░" * width
    filled = round(width * done / total)
    return "▓" * filled + "░" * (width - filled)


def _cut(s: str, n: int = 46) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _list(tasks: list[Task], limit: int | None, newest_last: bool = False, show_owner: bool = False) -> list[str]:
    items = tasks[-limit:] if (limit and newest_last) else (tasks[:limit] if limit else tasks)
    out = []
    for t in items:
        owner = f" <i>· {html.escape(t.owner)}</i>" if show_owner and t.owner and t.owner not in ("-", "–") else ""
        out.append(f"  • <code>{html.escape(t.id)}</code> {html.escape(_cut(t.title))}{owner}")
    hidden = len(tasks) - len(items)
    if hidden > 0:
        out.insert(0, f"  <i>… {hidden} earlier</i>") if newest_last else out.append(f"  <i>… +{hidden} more</i>")
    return out


def render(project: str, mode: str, tests_line: str, board_md: str | None, spec_md: str | None,
           extra_lines: list[str], full: bool = False) -> str:
    tasks = parse_board(board_md or "")
    feats = parse_features(spec_md or "")
    groups = {g: [t for t in tasks if t.group == g] for g in ("done", "active", "blocked", "todo")}
    n_done, n_all = len(groups["done"]), len(tasks)

    lines = [f"<b>📋 {html.escape(project)}</b>", f"{mode}", tests_line, ""]
    if n_all:
        pct = round(100 * n_done / n_all)
        lines += [f"<b>Tasks</b>  {_bar(n_done, n_all)}  {n_done}/{n_all} done ({pct}%)", ""]
        limit = None if full else 5
        if groups["active"]:
            lines += [f"🔄 <b>In progress ({len(groups['active'])})</b>"] + _list(groups["active"], None, show_owner=True) + [""]
        if groups["blocked"]:
            lines += [f"⛔ <b>Blocked ({len(groups['blocked'])})</b>"] + _list(groups["blocked"], None, show_owner=True) + [""]
        if groups["todo"]:
            lines += [f"⏳ <b>To do ({len(groups['todo'])})</b>"] + _list(groups["todo"], None if full else 6) + [""]
        if groups["done"]:
            lines += [f"✅ <b>Done ({n_done})</b>"] + _list(groups["done"], limit, newest_last=True) + [""]
    else:
        lines += ["<i>No tasks on the board yet. The PM creates them after planning.</i>", ""]

    if feats:
        lines.append("<b>🗺 Plan</b>")
        for name, d, t in feats:
            icon = "✅" if t and d == t else ("🔄" if d else "⏳")
            count = f"  {d}/{t}" if t else ""
            lines.append(f"  {icon} {html.escape(_cut(name, 40))}<i>{count}</i>")
        lines.append("")

    lines += [l for l in extra_lines if l]
    if not full and n_all > 11:
        lines.append("<i>/status all shows every task</i>")
    return "\n".join(lines).strip()
