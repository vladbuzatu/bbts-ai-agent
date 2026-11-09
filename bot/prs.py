"""Pull requests from Telegram: a card per PR with Merge / Request changes / Close, and GitHub
activity (comments, reviews, merges done on the website) forwarded to the PM.

State: orch.state.prs = {"<project>#<n>": {project, number, url, branch, title, seen_comments: [...],
                                           state: open|merged|closed}}
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator import Orchestrator

MARK = "<!-- bbts-bot -->"          # comments the bot itself posts (never forwarded back)
POLL_EVERY_S = 300


class PullRequests:
    def __init__(self, orch: "Orchestrator"):
        self.o = orch
        self.awaiting_feedback: str | None = None    # PR key whose change request the owner is typing
        self._last_poll = 0.0

    @property
    def st(self) -> dict:
        return self.o.state.prs

    def _repo(self, project: str) -> str | None:
        return self.o.project_info(project).get("repo")

    async def _gh(self, project: str, *args: str, timeout: int = 120) -> tuple[int, str]:
        return await self.o.repos._x(project, "gh", *args, timeout=timeout)

    # ------------------------------------------------------------- the card
    async def opened(self, project: str, url: str, branch: str, commits: str, title: str = "") -> None:
        """Called after the bot pushed a branch and its PR exists: record it and send the card."""
        m = re.search(r"/pull/(\d+)", url)
        if not m:
            return
        n = int(m.group(1))
        key = f"{project}#{n}"
        rec = self.st.setdefault(key, {"project": project, "number": n, "url": url, "branch": branch,
                                       "seen_comments": [], "state": "open", "title": title or branch})
        rec.update({"url": url, "branch": branch, "state": "open", "updated": time.time()})
        self.o._save_state()
        text, buttons = await self.card(key, commits)
        await self.o.notify(text, buttons=buttons)
        shots = getattr(self.o, "visual", None)
        if shots:
            await shots.send_for_pr(project, key)

    async def card(self, key: str, commits: str = "") -> tuple[str, list]:
        rec = self.st[key]
        project, n = rec["project"], rec["number"]
        code, out = await self._gh(project, "pr", "view", str(n), "-R", self._repo(project) or "",
                                   "--json", "title,additions,deletions,changedFiles,files")
        info = json.loads(out) if code == 0 and out.startswith("{") else {}
        files = [f.get("path", "") for f in info.get("files", [])][:8]
        verify = self.o.project_info(project).get("last_verify") or (
            self.o.state.last_verify if project == self.o.main_name else "")
        tests = "✅ tests pass" if "VERIFY: PASS" in verify else "❌ tests failing" if verify else "tests: not run"
        lines = [f"🔀 Pull request #{n} · {self.o.project_root(project)[0]}",
                 info.get("title") or rec.get("title", ""),
                 f"{rec['branch']} · {commits + ' commit(s) · ' if commits else ''}"
                 f"{info.get('changedFiles', '?')} files (+{info.get('additions', '?')} −{info.get('deletions', '?')}) · {tests}"]
        if files:
            lines.append("Files: " + ", ".join(f.rsplit("/", 1)[-1] for f in files)
                         + (" …" if info.get("changedFiles", 0) > len(files) else ""))
        lines.append(rec["url"])
        buttons = [[("✅ Merge", f"pr:merge:{key}"), ("💬 Request changes", f"pr:changes:{key}")],
                   [("❌ Close", f"pr:close:{key}")]]
        return "\n".join(lines), buttons

    # -------------------------------------------------------------- actions
    async def merge(self, key: str) -> str:
        rec = self.st.get(key)
        if not rec or rec.get("state") != "open":
            return "That pull request isn't open any more."
        project = rec["project"]
        code, out = await self._gh(project, "pr", "merge", str(rec["number"]), "-R", self._repo(project),
                                   "--merge", "--delete-branch", timeout=180)
        if code != 0:
            return f"⚠️ Couldn't merge #{rec['number']}: {out[-300:]}"
        await self._after_merge(key)
        return f"✅ Merged #{rec['number']} into main. The agents' copy is being updated."

    async def _after_merge(self, key: str) -> None:
        rec = self.st[key]
        rec["state"] = "merged"
        self.o._save_state()
        project = rec["project"]
        self.o.state.inbox.append(
            f"The owner merged pull request #{rec['number']} ({project}, branch {rec['branch']}). Switch "
            f"{project} back to main, call update_project_from_github, and start the next feature branch from there.")
        self.o._save_state()
        self.o._log(f"PR MERGED {key}")
        vis = getattr(self.o, "visual", None)
        if vis:
            vis.accept_baseline(project)

    async def close(self, key: str) -> str:
        rec = self.st.get(key)
        if not rec or rec.get("state") != "open":
            return "That pull request isn't open any more."
        project = rec["project"]
        code, out = await self._gh(project, "pr", "close", str(rec["number"]), "-R", self._repo(project),
                                   "--comment", f"Closed by the owner from Telegram. {MARK}")
        if code != 0:
            return f"⚠️ Couldn't close #{rec['number']}: {out[-300:]}"
        rec["state"] = "closed"
        self.o.state.inbox.append(f"The owner closed pull request #{rec['number']} ({project}) without merging. "
                                  "Ask them what to do with that work before continuing on it.")
        self.o._save_state()
        return f"❌ Closed #{rec['number']}."

    def ask_feedback(self, key: str) -> str:
        if key not in self.st:
            return "I don't know that pull request any more."
        self.awaiting_feedback = key
        return (f"💬 What should change in #{self.st[key]['number']}? Write it in your next message "
                "(it goes to the PM and as a comment on the pull request).")

    async def feedback(self, text: str) -> str:
        key, self.awaiting_feedback = self.awaiting_feedback, None
        rec = self.st.get(key or "")
        if not rec:
            return "Lost track of that pull request; send your feedback to the PM directly."
        project = rec["project"]
        await self._gh(project, "pr", "comment", str(rec["number"]), "-R", self._repo(project),
                       "--body", f"**Owner's change request** (via Telegram):\n\n{text}\n\n{MARK}")
        self.o.state.inbox.append(f"The owner requested changes on pull request #{rec['number']} ({project}, branch "
                                  f"{rec['branch']}): {text}\nMake them on the same branch; the bot updates the PR.")
        self.o._save_state()
        started = self.o.start_build([project])
        return (f"📨 Sent to the PM and posted on #{rec['number']}."
                + (f" Build started for {project}." if started else " The PM picks it up in its next round."))

    # ---------------------------------------------------------------- polling
    async def poll(self, force: bool = False) -> None:
        """Forward new GitHub comments/reviews to the PM; notice merges/closures done on the website."""
        if not force and time.time() - self._last_poll < POLL_EVERY_S:
            return
        self._last_poll = time.time()
        for key, rec in list(self.st.items()):
            if rec.get("state") != "open":
                continue
            project, n = rec["project"], rec["number"]
            repo = self._repo(project)
            if not repo:
                continue
            code, out = await self._gh(project, "pr", "view", str(n), "-R", repo,
                                       "--json", "state,comments,reviews")
            if code != 0 or not out.startswith("{"):
                continue
            info = json.loads(out)
            items = [(c.get("id") or c.get("url"), c.get("author", {}).get("login", "?"), c.get("body", ""))
                     for c in info.get("comments", [])]
            items += [(r.get("id"), r.get("author", {}).get("login", "?"),
                       f"[review: {r.get('state', '').lower()}] {r.get('body', '')}")
                      for r in info.get("reviews", []) if r.get("body") or r.get("state") == "CHANGES_REQUESTED"]
            new = [(i, who, body) for i, who, body in items
                   if i and str(i) not in rec["seen_comments"] and MARK not in body]
            for i, who, body in new:
                rec["seen_comments"].append(str(i))
                self.o.state.inbox.append(f"GitHub comment on pull request #{n} ({project}) by {who}: {body[:2000]}")
                await self.o.notify(f"💬 {who} on #{n} ({project}): {body[:300]}\n→ forwarded to the PM.")
            state = info.get("state", "").upper()
            if state == "MERGED":
                await self._after_merge(key)
                await self.o.notify(f"✅ #{n} ({project}) was merged on GitHub. The PM will update its copy.")
            elif state == "CLOSED":
                rec["state"] = "closed"
            self.o._save_state()
