"""Preview site (SITE_DOMAIN): the bot's side.

One public address (not indexed by search engines or AI crawlers) that shows whichever project the
owner or the PM deployed last. The bot can't run Docker, so it hands the host a snapshot of the
project's committed code (git archive of HEAD) and a request in /control; the host's ops/site.sh
checks the project's preview/compose.yml, builds and starts it, and points the address at it
(or keeps the previous version if the new one doesn't start). Status comes back in
/control/site-status.json.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import hostctl

COMPOSE = "preview/compose.yml"
FINAL = ("ok", "failed", "stopped")
BUSY = ("queued", "building", "starting")
STALE_S = 45 * 60          # a deploy can't take longer than this (the host gives up at ~25 min of building)


def domain() -> str:
    return os.environ.get("SITE_DOMAIN", "").strip().rstrip("/")


def url() -> str:
    d = domain()
    return d if d.startswith(("http://", "https://")) else f"https://{d}" if d else ""


def _control() -> Path:
    return hostctl.CONTROL


def read_status() -> dict:
    try:
        return json.loads((_control() / "site-status.json").read_text())
    except (OSError, ValueError):
        return {}


def _write_status(**fields) -> None:
    path = _control() / "site-status.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**fields, "at": int(time.time()), "url": url()}))
    os.replace(tmp, path)


def problem() -> str | None:
    """Why a deploy can't happen right now (setup), or None."""
    if not domain():
        return ("The preview site isn't set up: put SITE_DOMAIN=<your-name>.duckdns.org in the kit's .env "
                "(the name must point at this server), then  docker compose up -d  in the kit folder.")
    if not (_control() / "site-ready").exists():
        return "The server's side of the preview site isn't installed: run  sudo ops/install.sh  in the kit folder."
    return None


def busy() -> str | None:
    if (_control() / "site-request").exists():
        return "A preview-site request is waiting for the server."
    s = read_status()
    if s.get("state") in BUSY and time.time() - float(s.get("at") or 0) < STALE_S:
        return f"The preview site is busy: {s.get('message') or s['state']}."
    return None


def _git(root: Path, *args: str) -> tuple[int, str]:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout or r.stderr).strip()


REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


def _resolve(ref: str | None, sources: list[tuple[str, Path]]) -> tuple[str, Path, str] | None:
    """(commit, git dir, where) for a branch/tag/commit: the team's copy first, then GitHub's."""
    for where, gitdir in sources:
        if not (gitdir / "HEAD").exists() and not (gitdir / ".git").exists():
            continue
        cands = ["HEAD"] if not ref else [f"refs/heads/{ref}", f"refs/remotes/origin/{ref}", f"refs/tags/{ref}", ref]
        for cand in cands:
            code, sha = _git(gitdir, "rev-parse", "--verify", "--quiet", f"{cand}^{{commit}}")
            if code == 0 and sha:
                return sha, gitdir, where
    return None


def request_deploy(name: str, root: Path, title: str = "", ref: str | None = None,
                   github: Path | None = None) -> tuple[str, str | None]:
    """Snapshot committed code (the project's current branch, or `ref`: a branch, tag or commit from the
    team's copy or GitHub) and ask the host to deploy it. (message, request id)"""
    if p := problem():
        return p, None
    if b := busy():
        return b + " Try again when it's done (/site shows progress).", None
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,29}", name or ""):
        return f"{name!r} isn't a valid project name.", None
    if ref and (not REF_RE.fullmatch(ref) or ".." in ref):
        return f"{ref!r} doesn't look like a branch, tag or commit.", None
    if not (root / ".git").exists():
        return f"{name} has no git repository at {root}.", None
    sources = [("the team's copy", root)] + ([("GitHub", github)] if github else [])
    found = _resolve(ref, sources)
    if not found:
        if ref:
            return (f"{name} has no branch, tag or commit {ref!r} (looked in the team's copy"
                    f"{' and on GitHub' if github else ''})."), None
        return f"{name} has no commits yet.", None
    commit, gitdir, where = found
    if _git(gitdir, "cat-file", "-e", f"{commit}:{COMPOSE}")[0] != 0:
        return (f"{name}{' (' + ref + ')' if ref else ''} has no {COMPOSE} committed yet, so the server doesn't "
                f"know how to run it. Ask the PM: \"prepare {name} for the preview site\" "
                "(see the preview-site skill)."), None
    dirty = "" if ref else _git(root, "status", "--porcelain", "--untracked-files=no")[1]
    ident = f"{int(time.time())}-{secrets.token_hex(3)}"
    outdir = _control() / "site"
    outdir.mkdir(mode=0o700, exist_ok=True)
    code, out = _git(gitdir, "archive", "--format=tar", "-o", str(outdir / f"{ident}.tar"), commit)
    if code != 0:
        return f"Couldn't snapshot {name}'s code: {out[-300:]}", None
    _, subject = _git(gitdir, "log", "-1", "--format=%s", commit)
    label = f"{ref} " if ref else ""
    req = {"id": ident, "action": "deploy", "project": name, "commit": commit,
           "title": (title or (f"[{ref}] " if ref else "") + subject)[:120]}
    tmp = _control() / "site-request.tmp"
    tmp.write_text(json.dumps(req))
    _write_status(state="queued", message=f"Deploying {name} {label}{commit[:7]}", id=ident, project=name,
                  commit=commit, title=req["title"])
    os.replace(tmp, _control() / "site-request")
    note = "\n⚠️ Uncommitted changes aren't included – only committed work is deployed." if dirty else ""
    src = f" from {where}" if ref else ""
    return (f"🚀 Deploying {name} {label}({commit[:7]} – {subject[:80]}){src} to {url()}.\n"
            f"The server builds it and switches the address once it answers (a few minutes; "
            f"the first build of a project takes longest). I'll report back.{note}"), ident


async def deploy(orch, name: str, ref: str | None = None) -> tuple[str, str | None]:
    """request_deploy for a project of the orchestrator; for a branch, GitHub is fetched first."""
    _, root = orch.project_root(name)
    github = None
    info = orch.project_info(name)
    if ref and info.get("repo"):
        github = orch.mirror if info.get("main") else orch.repos.mirror(name)
        if (github / "HEAD").exists():      # newest branches from GitHub, kept apart from the bot's own refs
            await orch.repos._x(name, "git", "-C", str(github), "fetch", "-q", "--prune", "origin",
                                "+refs/heads/*:refs/remotes/origin/*", timeout=300)
        else:
            github = None
    return await asyncio.to_thread(request_deploy, name, root, "", ref or None, github)


def request_stop() -> str:
    if p := problem():
        return p
    if b := busy():
        return b
    (_control() / "site-request.tmp").write_text(json.dumps({"id": f"stop-{secrets.token_hex(4)}", "action": "stop"}))
    os.replace(_control() / "site-request.tmp", _control() / "site-request")
    return "⏹ Stopping the preview site (each project's data is kept)."


def log_tail(lines: int = 60) -> str:
    try:
        text = (_control() / "site-log.txt").read_text(errors="replace")
    except OSError:
        return "No preview-site deploy has run yet."
    tail = "\n".join(text.splitlines()[-lines:])
    return tail or "The last deploy wrote no output."


def summary() -> str:
    if not domain():
        return problem() or ""
    s = read_status()
    lines = [f"🌐 Preview site: {url()}"]
    state = s.get("state")
    what = f"{s.get('project')} {str(s.get('commit') or '')[:7]}".strip()
    ago = hostctl._ago(s.get("at"))
    if state == "ok":
        lines.append(f"✅ Live: {what} – {s.get('title') or ''} ({ago})")
    elif state in BUSY:
        lines.append(f"⏳ {s.get('message') or state} ({ago})")
    elif state == "failed":
        lines.append(f"❌ Last deploy failed ({what}, {ago}): {s.get('message', '')}")
    elif state == "stopped":
        lines.append(f"⏹ Stopped ({ago})")
    else:
        lines.append("Nothing deployed yet.")
    if p := problem():
        lines.append(p)
    lines.append("\n/site <project> [branch] deploys · /site log · /site stop")
    return "\n".join(lines)


async def wait(ident: str | None, seconds: float, poll: float = 3.0) -> dict:
    """Wait until the request `ident` (or whatever is running) is finished, up to `seconds`."""
    deadline = time.time() + seconds
    while True:
        s = read_status()
        mine = not ident or s.get("id") == ident
        if mine and s.get("state") in FINAL and not (_control() / "site-request").exists():
            return s
        if time.time() >= deadline:
            return s
        await asyncio.sleep(poll)


def result_text(s: dict, with_log: bool = True) -> str:
    state = s.get("state")
    if state == "ok":
        return (f"✅ {s.get('project')} ({str(s.get('commit'))[:7]}) is live at {s.get('url') or url()}. "
                "Open it with the browser tools to check it.")
    if state == "failed":
        return f"❌ Deploy failed: {s.get('message')}" + (f"\n\nLast lines of the deploy log:\n{log_tail(40)}" if with_log else "")
    if state == "stopped":
        return "The preview site is stopped."
    return f"⏳ Still running: {s.get('message') or state or 'waiting for the server'}. Check again in a minute."


def make_tools(orch) -> list:
    """Team tools: the PM (and agents) deploy a project to the preview site and wait for the result."""
    from claude_agent_sdk import tool

    @tool("deploy_preview_site",
          "Deploy a project's COMMITTED code to the shared preview site (SITE_DOMAIN) so the owner can see "
          "it – replaces whatever the site showed before. The project needs preview/compose.yml (see "
          ".claude/skills/preview-site). Commit first. Waits up to ~5 minutes; if it's still building, call "
          "preview_site_status. name: project id (e.g. tstauto). branch: optional branch, tag or commit "
          "(the team's copy first, then GitHub); empty = the project's current branch.",
          {"name": str, "branch": str})
    async def deploy_preview_site(args):
        name = (args.get("name") or "").strip().lower() or orch.state.active_project or orch.main_name
        if name not in orch.project_names():
            return {"content": [{"type": "text", "text": f"Unknown project {name!r}. Known: {', '.join(orch.project_names())}"}]}
        text, ident = await deploy(orch, name, (args.get("branch") or "").strip() or None)
        if ident:
            orch._log(f"SITE deploy {name} requested by the agents")
            text = result_text(await wait(ident, 300))
        return {"content": [{"type": "text", "text": text}]}

    @tool("preview_site_status",
          "Status of the preview site: what's live, or how the last deploy went (with its log when it "
          "failed). wait_minutes (0-5): wait that long for a running deploy to finish.",
          {"wait_minutes": int})
    async def preview_site_status(args):
        minutes = max(0, min(5, int(args.get("wait_minutes") or 0)))
        s = await wait(None, minutes * 60) if minutes else read_status()
        text = (problem() or "") if not s else result_text(s)
        return {"content": [{"type": "text", "text": text or "Nothing has been deployed to the preview site yet."}]}

    return [deploy_preview_site, preview_site_status]
