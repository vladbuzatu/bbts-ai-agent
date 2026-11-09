"""Other projects' own GitHub repos (e.g. CarStage), next to the main project.

Same safety model as the main project:
- Agents never get a GitHub token and never push. They work on a normal clone in the workspace.
- The bot keeps a bare mirror per project in its private data folder. It brings GitHub's code to the
  agents through a git *bundle* file (no credentials, no network for the agents), and it publishes the
  agents' work by fetching their commits into the mirror and pushing a branch: `agents/work`.
- It never pushes `main`. Changes reach `main` only when the owner merges the pull request on GitHub.
- Root never runs git inside the agents' folder (only fetches from it into the mirror).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
WORK_BRANCH = "agents/work"
SECRET_RE = re.compile(r"AKIA[0-9A-Z]{16}|github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{30,}|"
                       r"AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[bp]-[A-Za-z0-9-]{20,}")


BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


def token_for(name: str) -> str | None:
    """GITHUB_TOKEN_<NAME> (e.g. GITHUB_TOKEN_CARSTAGE) if set, else the bot's GITHUB_TOKEN."""
    return sandbox.secret(f"GITHUB_TOKEN_{name.upper().replace('-', '_')}") or sandbox.secret("GITHUB_TOKEN")


class ProjectRepos:
    def __init__(self, orch: "Orchestrator"):
        self.o = orch

    async def _x(self, name: str, *cmd: str, timeout: int = 300) -> tuple[int, str]:
        """A bot command for one project, with that project's GitHub token (env only, never argv)."""
        return await self.o._exec(*cmd, timeout=timeout, token=token_for(name))

    def write_credentials(self) -> None:
        """git credentials per repository (so each repo can have its own token)."""
        cred = Path(os.environ.get("BBTS_GIT_CREDENTIALS") or (Path.home() / ".git-credentials"))
        lines = []
        repos = {}
        if self.o.cfg.github_repo:
            repos[self.o.cfg.github_repo] = token_for(self.o.main_name)
        for name, info in self.o.state.projects.items():
            if info.get("repo"):
                repos[info["repo"]] = token_for(name)
        for repo, tok in repos.items():
            if tok:
                lines.append(f"https://x-access-token:{tok}@github.com/{repo}.git")
        if not lines:
            return
        cred.write_text("\n".join(lines) + "\n")
        cred.chmod(0o600)
        import subprocess
        subprocess.run(["git", "config", "--global", "credential.helper", f"store --file={cred}"], check=False)
        subprocess.run(["git", "config", "--global", "credential.useHttpPath", "true"], check=False)

    def mirror(self, name: str) -> Path:
        return self.o.cfg.data_dir / "mirrors" / f"{name}.git"

    def _info(self, name: str) -> dict | None:
        return self.o.state.projects.get(name)

    def _folder(self, info: dict) -> Path:
        if info.get("main"):
            return self.o.cfg.workspace
        return (self.o.cfg.master if self.o.cfg.multi else self.o.cfg.workspace) / info["path"]

    async def ensure_mirror(self, name: str, repo: str) -> str | None:
        m = self.mirror(name)
        if (m / "HEAD").exists():
            return None
        m.parent.mkdir(parents=True, exist_ok=True)
        url = os.environ.get("PROJECTS_GIT_URL", "https://github.com/{repo}.git").format(repo=repo)
        code, out = await self._x(name, "git", "clone", "-q", "--bare", url, str(m), timeout=600)
        if code != 0:
            shutil.rmtree(m, ignore_errors=True)
            return out[-300:]
        return None

    async def _agit(self, folder: Path, *args: str) -> tuple[int, str]:
        r = await asyncio.to_thread(sandbox.git, *args, cwd=folder)
        return r.returncode, (r.stdout + r.stderr).strip()

    async def _bundle(self, name: str) -> tuple[Path | None, str]:
        """A read-only snapshot of the mirror the agent user can fetch from."""
        b = Path("/tmp") / f"{name}-{int(time.time())}.bundle"
        code, out = await self.o._exec("git", "-C", str(self.mirror(name)), "bundle", "create", str(b), "--all")
        if code != 0:
            return None, out
        b.chmod(0o644)
        return b, ""

    # ------------------------------------------------------------------ link
    async def link(self, name: str, repo: str) -> str:
        name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:30]
        if not name or name == "main":
            return "Usage: /project link <name> <owner/repo>   e.g. /project link carstage vladbuzatu/carstage"
        if not REPO_RE.match(repo):
            return f"{repo!r} doesn't look like owner/repo."
        if not token_for(name):
            return "No GITHUB_TOKEN in .env: the bot can't reach GitHub."
        default_path = f"projects/{name}" if self.o.cfg.multi else name
        info = self.o.state.projects.setdefault(name, {"path": default_path, "title": name.capitalize()})
        info.setdefault("mode", "pr")
        info["repo"] = repo
        self.write_credentials()
        folder = self._folder(info)
        m = self.mirror(name)
        m.parent.mkdir(parents=True, exist_ok=True)
        url = os.environ.get("PROJECTS_GIT_URL", "https://github.com/{repo}.git").format(repo=repo)
        if not (m / "HEAD").exists():
            code, out = await self._x(name, "git", "clone", "-q", "--bare", url, str(m), timeout=600)
            if code != 0:
                shutil.rmtree(m, ignore_errors=True)
                return ("❌ Couldn't read the repo from GitHub. Check that the name is right and that the bot's "
                        f"GitHub token has access to {repo} (Contents + Pull requests: read and write).\n"
                        + out[-300:])
        else:
            await self._x(name, "git", "-C", str(m), "remote", "set-url", "origin", url)
            await self._x(name, "git", "-C", str(m), "fetch", "-q", "origin", "+refs/heads/*:refs/heads/*", timeout=600)
        code, branch = await self.o._exec("git", "-C", str(m), "symbolic-ref", "--short", "HEAD")
        branch = branch.strip() if code == 0 and branch.strip() else "main"
        moved = ""
        if folder.exists() and not (folder / ".git").exists() and any(folder.iterdir()):
            moved = await self._adopt_folder(name, folder, branch)
            if moved.startswith("❌"):
                return moved
        if not (folder / ".git").exists():
            bundle, err = await self._bundle(name)
            if not bundle:
                return f"❌ Couldn't prepare the code for the agents: {err[-300:]}"
            try:
                base_dir = self.o.cfg.master if self.o.cfg.multi else self.o.cfg.workspace
                folder.parent.mkdir(parents=True, exist_ok=True)
                sandbox.give_to_agent(folder.parent)
                code, out = await self._agit(base_dir, "clone", "-q", "-b", branch, str(bundle), info["path"])
            finally:
                bundle.unlink(missing_ok=True)
            if code != 0:
                return f"❌ Clone for the agents failed: {out[-300:]}"
            await self._agit(folder, "remote", "remove", "origin")
        info.update({"repo": repo, "base": branch})
        self.o.state.projects[name] = info
        self.o._save_state()
        note = "" if self.o.cfg.multi else await asyncio.to_thread(self.o._keep_out_of_main_repo, info["path"])
        self.o._log(f"PROJECT LINK {name} -> {repo}")
        return (f"🔗 {info.get('title', name)} is linked to github.com/{repo} ({branch}).\n"
                f"The agents work in {info['path']}/ on feature branches. After green tests the bot pushes the branch "
                f"and opens a pull request for you to review (or /project push {name}); {branch} only changes "
                "when you merge it."
                + moved + note)

    async def _adopt_folder(self, name: str, folder: Path, branch: str) -> str:
        """The agents already have files in the folder (not a git repo): connect it to GitHub's history
        without losing their work. Their changes become a commit on a feature branch; files that only
        exist on GitHub are restored, never deleted."""
        backup = self.o.cfg.studio_dir / "backups" / f"{name}-{time.strftime('%Y%m%d-%H%M')}"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(folder, backup, symlinks=True, ignore=shutil.ignore_patterns("node_modules", ".venv", "venv"))
        sandbox.give_to_agent(backup.parent)
        tmp = folder.parent / f".{name}-linking"
        shutil.rmtree(tmp, ignore_errors=True)
        bundle, err = await self._bundle(name)
        if not bundle:
            return f"❌ Couldn't prepare the code: {err[-300:]}"
        try:
            code, out = await self._agit(folder.parent, "clone", "-q", "--no-checkout", "-b", branch, str(bundle), tmp.name)
        finally:
            bundle.unlink(missing_ok=True)
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            return f"❌ Clone for the agents failed: {out[-300:]}"
        shutil.move(str(tmp / ".git"), str(folder / ".git"))
        shutil.rmtree(tmp, ignore_errors=True)
        sandbox.give_to_agent(folder)
        await self._agit(folder, "remote", "remove", "origin")
        await self._agit(folder, "reset", "-q")                        # index = GitHub's version
        await self._agit(folder, "add", "--ignore-removal", ".")       # new + changed files, no deletions
        code, staged = await self._agit(folder, "diff", "--cached", "--name-only")
        await self._agit(folder, "checkout", "--", ".")                # restore files only GitHub has
        files = [f for f in staged.splitlines() if f.strip()]
        if not files:
            return f"\nThe existing {folder.name}/ matched GitHub's {branch}; nothing to keep separately."
        wip = f"feature/{time.strftime('%Y%m%d')}-agents-work"
        await self._agit(folder, "switch", "-q", "-c", wip)
        code, out = await self._agit(folder, "commit", "-q", "-m",
                                     "Agents' work in progress from before the repo was linked")
        if code != 0:
            return f"\n⚠️ Linked, but couldn't commit the existing work: {out[-200:]}"
        return (f"\nThe agents' existing work ({len(files)} changed/new file(s)) is kept as a commit on the branch "
                f"{wip}; files that only exist on GitHub were restored. A copy of the old folder is in {backup}. "
                f"/project push {name} opens the pull request.")

    # --------------------------------------------------------------- publish
    async def publish(self, name: str, title: str = "", body: str = "", allow_workflows: bool = False) -> str:
        """Push the agents' current feature branch and open/update its pull request."""
        info = self.o.project_info(name) if name == self.o.main_name else self._info(name)
        if not info or not info.get("repo"):
            return f"{name!r} isn't linked to a GitHub repo (/project link {name} <owner/repo>)."
        folder, m, repo, base = self._folder(info), str(self.mirror(name)), info["repo"], info.get("base", "main")
        err = await self.ensure_mirror(name, repo)
        if err:
            return f"⚠️ Couldn't reach {repo} on GitHub: {err}"
        code, branch = await self._agit(folder, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch.strip()
        if code != 0 or not BRANCH_RE.match(branch) or branch == "HEAD":
            return f"⚠️ Couldn't tell which branch the agents are on in {info['path']}/ ({branch[:80]})."
        target = WORK_BRANCH if branch == base else branch       # never push the base branch itself
        if not re.match(r"^(feature|fix|agents)/", target):
            return (f"⚠️ Not pushed: the agents are on '{branch}'. The bot only publishes feature/…, fix/… or "
                    "agents/… branches (so it can never overwrite other branches on GitHub).")
        code, status = await self._agit(folder, "status", "--porcelain")
        uncommitted = len([line for line in status.splitlines() if line.strip()]) if code == 0 else 0
        code, out = await self._x(name, "git", "-C", m, "fetch", "-q", "--no-tags", f"file://{folder.resolve()}",
                                  f"+refs/heads/{branch}:refs/heads/{target}")
        if code != 0:
            return f"⚠️ Couldn't read the agents' commits: {out[-300:]}"
        await self._x(name, "git", "-C", m, "fetch", "-q", "origin", f"+refs/heads/{base}:refs/heads/{base}")
        code, ahead = await self._x(name, "git", "-C", m, "rev-list", "--count", f"{base}..{target}")
        if code == 0 and ahead.strip() == "0":
            return (f"Nothing new to publish: {branch} has no commits beyond {base}."
                    + (f" ({uncommitted} uncommitted change(s): ask the PM to commit them.)" if uncommitted else ""))
        if target not in info.get("pushed_branches", []):
            code, remote = await self._x(name, "git", "-C", m, "ls-remote", "--heads", "origin", target)
            if code == 0 and remote.strip():
                return (f"⚠️ Not pushed: {target} already exists on GitHub and wasn't made by the bot. "
                        "Ask the PM to use a new branch name.")
        workflows = await self._touches_workflows(m, base, target)
        if workflows and not allow_workflows:
            return ("⛔ Not pushed: these commits change GitHub Actions workflows ("
                    + ", ".join(workflows[:4]) + "). Workflows run with the repo's secrets, so they need your OK: "
                    f"review them, then send /project push {name} workflows.")
        hits, err = await self._new_secrets(name, m, base, target)
        if err:
            return f"⛔ Not pushed: {err}, so the commits couldn't be checked for secrets."
        if hits:
            self.o._log(f"PROJECT PUSH BLOCKED {name}: secret-like strings {hits}")
            return ("⛔ Not pushed: the new commits contain something that looks like a secret key "
                    f"({', '.join(hits)}). Ask the PM to remove it from the commits (not just the files) first.")
        code, out = await self._x(name, "git", "-C", m, "push", "-q", "--force", "origin",
                                  f"refs/heads/{target}:refs/heads/{target}", timeout=300)
        if code != 0:
            return f"⚠️ Push to GitHub failed: {out[-400:]}"
        code, out = await self._x(name, "gh", "pr", "list", "-R", repo, "--head", target, "--state", "open",
                                  "--json", "url", "-q", ".[0].url")
        url = out.strip() if code == 0 else ""
        if not url:
            code, out = await self._x(
                name, "gh", "pr", "create", "-R", repo, "--base", base, "--head", target,
                "--title", (title or f"{target}: agents' changes ({time.strftime('%d %b %Y')})")[:120],
                "--body", (body or "Changes made by the agent team; tests passed on the server. Review before merging.")[:6000])
            url = out.strip().splitlines()[-1] if code == 0 and out.strip() else ""
            if code != 0:
                return f"⬆️ Pushed {target}, but couldn't open the pull request: {out[-300:]}"
        rec = self.o.state.projects.get(name)
        if rec is not None:
            pushed = rec.setdefault("pushed_branches", [])
            if target not in pushed:
                pushed.append(target)
            self.o._save_state()
        self.o._log(f"PROJECT PUSH {name} -> {repo}:{target} ({ahead.strip()} commits)")
        extra = f" ({uncommitted} uncommitted change(s) not included.)" if uncommitted else ""
        try:
            await self.o.prs.opened(name, url, target, ahead.strip(), title)
        except Exception as e:      # the push worked; a card problem must not hide that
            self.o._log(f"PR card failed: {e!r}")
        return f"⬆️ {ahead.strip()} commit(s) on {target}. Review and merge: {url}{extra}"

    async def _git_bytes(self, m: str, *args: str, stdin: bytes | None = None) -> tuple[int, bytes]:
        """git on a bot-owned bare mirror, with stdin, raw bytes out (root; never an agent folder)."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", m, *args, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate(stdin)
        return proc.returncode, out

    async def _range(self, m: str, base: str, target: str) -> tuple[str, list[str]]:
        code, _ = await self._git_bytes(m, "rev-parse", "--verify", "-q", f"refs/heads/{base}")
        return ("ok", [target, f"^refs/heads/{base}"]) if code == 0 else ("no-base", [target])

    async def _new_secrets(self, name: str, m: str, base: str, target: str) -> tuple[list[str], str | None]:
        """Secret-looking strings in ANY file version the new commits introduce (also files added and
        removed again, and binary files), unless the value is already on the base branch.
        Returns (findings, error). Any error means: don't push (fail closed)."""
        has_base, rev = await self._range(m, base, target)
        code, objs = await self._git_bytes(m, "rev-list", "--objects", *rev)
        if code != 0:
            return [], "couldn't list the new commits"
        shas = [line.split(b" ", 1)[0] for line in objs.splitlines() if line.strip()]
        code, kinds = await self._git_bytes(m, "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                                            stdin=b"\n".join(shas) + b"\n")
        if code != 0:
            return [], "couldn't read the new files"
        blobs = [ln.split()[0] for ln in kinds.splitlines()
                 if len(ln.split()) == 3 and ln.split()[1] == b"blob" and int(ln.split()[2]) <= 20 * 1024 * 1024]
        found: set[str] = set()
        for i in range(0, len(blobs), 500):
            code, data = await self._git_bytes(m, "cat-file", "--batch", stdin=b"\n".join(blobs[i:i + 500]) + b"\n")
            if code != 0:
                return [], "couldn't read the new files"
            found |= set(SECRET_RE.findall(data.decode("latin-1")))
        leaks, err = await self._gitleaks(name, m, base if has_base == "ok" else "", target)
        if err:
            return [], err
        found |= leaks
        new = []
        for value in sorted(found):
            if has_base == "ok":
                code, _ = await self._git_bytes(m, "grep", "-q", "-F", "-e", value, f"refs/heads/{base}")
                if code == 0:
                    continue                    # already on main: not something the agents added
            new.append(value[:12] + "…")
        return new, None

    async def _gitleaks(self, name: str, m: str, base: str, target: str) -> tuple[set[str], str | None]:
        """gitleaks' rules (hundreds of key formats) over the new commits, when it's installed."""
        if not shutil.which("gitleaks"):
            return set(), None
        import json as _json
        import tempfile
        tmp = Path(tempfile.mkdtemp(dir=self.o.cfg.data_dir))
        report = tmp / "gitleaks.json"
        try:
            code, out = await self._x(name, "gitleaks", "git", m, "--log-opts", f"{base}..{target}" if base else target,
                                      "--no-banner", "--report-format", "json", "--report-path", str(report),
                                      "--exit-code", "0", timeout=300)
            if code != 0 or not report.exists():
                return set(), f"the secret scanner failed ({out[-150:]})"
            findings = _json.loads(report.read_text() or "[]")
        except (OSError, ValueError) as e:
            return set(), f"the secret scanner failed ({e})"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return {f.get("Secret", "") for f in findings if len(f.get("Secret", "")) >= 8}, None

    async def _touches_workflows(self, m: str, base: str, target: str) -> list[str]:
        has_base, rev = await self._range(m, base, target)
        if has_base == "ok":
            code, out = await self._git_bytes(m, "diff", "--name-only", f"refs/heads/{base}...{target}")
        else:
            code, out = await self._git_bytes(m, "ls-tree", "-r", "--name-only", target)
        return [f for f in out.decode(errors="replace").splitlines() if f.startswith(".github/workflows/")]

    # ------------------------------------------------------------------ pull
    async def pull(self, name: str) -> str:
        info = self._info(name)
        if not info or not info.get("repo"):
            return f"{name!r} isn't linked to a GitHub repo."
        folder, base = self._folder(info), info.get("base", "main")
        code, out = await self._x(name, "git", "-C", str(self.mirror(name)), "fetch", "-q", "origin",
                                  "+refs/heads/*:refs/heads/*", timeout=600)
        if code != 0:
            return f"⚠️ Couldn't fetch from GitHub: {out[-300:]}"
        bundle, err = await self._bundle(name)
        if not bundle:
            return f"⚠️ {err[-300:]}"
        try:
            code, out = await self._agit(folder, "fetch", "-q", str(bundle), f"+refs/heads/{base}:refs/remotes/github/{base}")
        finally:
            bundle.unlink(missing_ok=True)
        if code != 0:
            return f"⚠️ {out[-300:]}"
        code, status = await self._agit(folder, "status", "--porcelain")
        if status.strip():
            return (f"Fetched GitHub's {base}, but not merged: the agents have uncommitted changes in "
                    f"{info['path']}/. Ask the PM to commit, then /project pull {name} again.")
        code, cur = await self._agit(folder, "rev-parse", "--abbrev-ref", "HEAD")
        if cur.strip() != base:
            return (f"⬇️ Fetched GitHub's {base} as github/{base}. The agents are on {cur.strip()}: switch to {base} "
                    f"and merge github/{base} before starting the next feature branch.")
        code, out = await self._agit(folder, "merge", "--no-edit", f"github/{base}")
        if code != 0:
            await self._agit(folder, "merge", "--abort")
            return (f"⚠️ GitHub's {base} conflicts with the agents' changes; nothing changed. Ask the PM: "
                    f"\"merge github/{base} into {info['path']} and resolve the conflicts\".")
        self.o._log(f"PROJECT PULL {name}")
        return f"⬇️ {info.get('title', name)} is up to date with GitHub's {base}."


    # ------------------------------------------------------- lab follows main
    async def refresh_lab(self, force: bool = False) -> str | None:
        """Keep the CarStage lab's code equal to GitHub's main (the owner-merged code).
        Returns a short note when the lab code changed, None otherwise."""
        import lab as lab_mod
        name = os.environ.get("LAB_PROJECT", "carstage")
        info = self._info(name)
        st = self.o.state.lab_code
        if not info or not info.get("repo") or not lab_mod.CODE_DIR.is_dir():
            return None
        if not force and time.time() - float(st.get("checked") or 0) < 60:
            return None
        st["checked"] = time.time()
        base, m = info.get("base", "main"), str(self.mirror(name))
        code, out = await self._x(name, "git", "-C", m, "fetch", "-q", "origin",
                                  f"+refs/heads/{base}:refs/heads/{base}", timeout=180)
        if code != 0:
            self.o._log(f"LAB REFRESH fetch failed: {out[-200:]!r}")
            return None
        code, sha = await self.o._exec("git", "-C", m, "rev-parse", f"refs/heads/{base}")
        sha = sha.strip()
        if code != 0 or (sha == st.get("main_sha") and not force):
            return None
        import tempfile
        work = Path(tempfile.mkdtemp(dir=self.o.cfg.data_dir))      # root-only: no symlink games
        tmp, tar = work / "tree", work / "export.tar"
        tmp.mkdir()
        try:
            code, out = await self.o._exec("git", "-C", m, "archive", "-o", str(tar), sha)
            if code != 0:
                return None
            await self.o._exec("tar", "-xf", str(tar), "-C", str(tmp))
            n = await asyncio.to_thread(lab_mod.sync_apply, tmp, lab_mod.CODE_DIR)
        except OSError as e:
            self.o._log(f"LAB REFRESH failed: {e}")
            return None
        finally:
            shutil.rmtree(work, ignore_errors=True)
        st.update({"source": f"GitHub {base}", "sha": sha, "main_sha": sha, "at": time.time()})
        self.o._save_state()
        self.o._log(f"LAB REFRESH {info['repo']}@{sha[:12]} ({n} file(s) changed)")
        return f"Lab code updated to {info['repo']} {base} @ {sha[:7]} ({n} file(s) changed)."
