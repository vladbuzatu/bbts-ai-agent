"""Start-up smoke test, run inside the built image by CI: a fresh multi-project install and an old
single-project install being migrated must both come up, and /status must render."""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT / "bot"))
import orchestrator as orch_mod  # noqa: E402


async def notify(text, buttons=None):
    print("notify:", text[:80])


def cfg(base: Path, **kw):
    return orch_mod.Config(workspace=base / "master" / "projects" / "app", root=base / "master",
                           data_dir=base / "data", template_dir=KIT / "workspace-template",
                           pm_prompt_file=KIT / "bot/prompts/pm.md", studio_dir=base / "studio",
                           studio_template=KIT / "studio-template", curator_worktree=base / "cwt",
                           github_repo="owner/app", **kw)


def main():
    base = Path(tempfile.mkdtemp(prefix="smoke-"))
    base.chmod(0o755)
    o = orch_mod.Orchestrator(cfg(base / "fresh"), notify)
    assert (o.cfg.master / "CLAUDE.md").exists() and (o.cfg.workspace / ".git").exists()
    assert "<b>" in o.status() and o._options().cwd == str(o.cfg.master)

    old = base / "old" / "master"
    old.mkdir(parents=True)
    shutil.copytree(KIT / "workspace-template", old, dirs_exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=old, check=True)
    subprocess.run(["git", "add", "-A"], cwd=old, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "old"], cwd=old, check=True)
    (base / "old" / "data").mkdir()
    (base / "old" / "data" / "state.json").write_text(json.dumps({"session_id": "s1"}))
    o2 = orch_mod.Orchestrator(cfg(base / "old"), notify)
    assert (old / "projects" / "app" / ".git").exists() and o2.state.session_id == "s1"
    asyncio.run(asyncio.sleep(0))
    print("smoke OK")


if __name__ == "__main__":
    main()
