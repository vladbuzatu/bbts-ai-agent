"""Other projects with their own GitHub repo: link, publish (branch + PR), pull; secrets blocked."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import run


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path, monkeypatch):
    """A fake GitHub: bare repo at <tmp>/gh/vlad/carstage.git with one commit on main."""
    bare = tmp_path / "gh" / "vlad" / "carstage.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(bare), str(seed))
    (seed / "backend").mkdir()
    (seed / "backend" / "app.py").write_text("print('hi')\n")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    monkeypatch.setenv("PROJECTS_GIT_URL", f"file://{tmp_path}/gh/{{repo}}.git")
    return bare


def test_link_publish_pull_roundtrip(make_orch, remote, tmp_path):
    o = make_orch()
    o.cfg.github_token = "t"
    ws = o.cfg.workspace
    (ws / "carstage").mkdir()
    (ws / "carstage" / "old.txt").write_text("zip copy")          # the old, non-git copy
    msg = run(o.repos.link("carstage", "vlad/carstage"))
    assert "linked to github.com/vlad/carstage" in msg and "kept as a commit" in msg
    folder = ws / "carstage"
    assert (folder / "backend" / "app.py").exists()                # restored from GitHub, not deleted
    assert (folder / "old.txt").read_text() == "zip copy"          # the agents' work is kept...
    assert sh("git", "log", "--format=%s", "-1", cwd=folder).startswith("Agents' work in progress")
    assert sh("git", "status", "--porcelain", cwd=folder) == ""
    assert sh("git", "remote", cwd=folder) == ""                   # agents have no remote to push to
    assert "on feature/" in run(o.repos.publish("carstage"))       # ...and goes out as a pull request
    sh("git", "switch", "-q", "main", cwd=folder)
    assert "Nothing new" in run(o.repos.publish("carstage"))

    (folder / "backend" / "app.py").write_text("print('fixed')\n")
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "C-01 fix", cwd=folder)
    out = run(o.repos.publish("carstage"))
    assert "1 commit(s) on agents/work" in out and "pull/7" in out
    assert sh("git", "--git-dir", str(remote), "log", "--format=%s", "-1", "agents/work") == "C-01 fix"
    assert sh("git", "--git-dir", str(remote), "log", "--format=%s", "-1", "main") == "init"   # main untouched

    # the owner merges on GitHub; pull brings it back
    seed = tmp_path / "seed"
    sh("git", "pull", "-q", "origin", "main", cwd=seed)
    sh("git", "fetch", "-q", "origin", "agents/work", cwd=seed)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "merge", "-q", "--no-edit", "FETCH_HEAD", cwd=seed)
    (seed / "README.md").write_text("owner edit\n")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "readme", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    assert "up to date" in run(o.repos.pull("carstage"))
    assert (folder / "README.md").read_text() == "owner edit\n"


def test_publish_blocks_secrets(make_orch, remote):
    o = make_orch()
    o.cfg.github_token = "t"
    run(o.repos.link("carstage", "vlad/carstage"))
    folder = o.cfg.workspace / "carstage"
    (folder / "settings.py").write_text("KEY = 'AKIAQ2FIABCDEFGHIJKL'\n")
    sh("git", "add", "-A", cwd=folder)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "oops", cwd=folder)
    out = run(o.repos.publish("carstage"))
    assert out.startswith("⛔ Not pushed")
    assert "agents/work" not in sh("git", "--git-dir", str(remote), "branch")


def test_link_rejects_bad_names(make_orch):
    o = make_orch()
    o.cfg.github_token = "t"
    assert "owner/repo" in run(o.repos.link("carstage", "not a repo"))
    assert "Usage" in run(o.repos.link("main", "a/b"))


def test_lab_follows_github_main(make_orch, remote, tmp_path, monkeypatch):
    import lab
    o = make_orch()
    o.cfg.github_token = "t"
    code = tmp_path / "labcode"
    (code / "backend" / "models").mkdir(parents=True)
    (code / "backend" / "models" / "yolov8n-seg.onnx").write_bytes(b"weights")
    (code / "backend" / "stale.py").write_text("old\n")
    monkeypatch.setattr(lab, "CODE_DIR", code)
    run(o.repos.link("carstage", "vlad/carstage"))
    note = run(o.repos.refresh_lab(force=True))
    assert note and "vlad/carstage main" in note
    assert (code / "backend" / "app.py").read_text() == "print('hi')\n"
    assert not (code / "backend" / "stale.py").exists()
    assert (code / "backend" / "models" / "yolov8n-seg.onnx").exists()         # weights kept
    assert run(o.repos.refresh_lab(force=False)) is None                       # checked < 60 s ago
    o.state.lab_code["checked"] = 0
    assert run(o.repos.refresh_lab()) is None                                  # main unchanged

    seed = tmp_path / "seed"                                                   # the owner merges something
    (seed / "backend" / "app.py").write_text("print('merged')\n")
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "merged", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    o.state.lab_code["checked"] = 0
    assert "updated" in run(o.repos.refresh_lab())
    assert (code / "backend" / "app.py").read_text() == "print('merged')\n"
    assert "GitHub main @" in o.studio.lab.version_text()


def test_secret_check_ignores_keys_already_on_main(make_orch, remote, tmp_path):
    seed = tmp_path / "seed"
    (seed / "index.html").write_text('<script src="maps.js?key=AIzaSyCAdABhEXISTINGBROWSERKEY12345678"></script>\n')
    sh("git", "add", "-A", cwd=seed)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "maps", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    o = make_orch()
    o.cfg.github_token = "t"
    run(o.repos.link("carstage", "vlad/carstage"))
    folder = o.cfg.workspace / "carstage"
    sh("git", "switch", "-q", "-c", "feature/redesign", cwd=folder)
    html = (folder / "index.html").read_text()
    (folder / "index.html").write_text('<link rel="stylesheet" href="fonts.css">\n' + html.replace("<script", "<script defer"))
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qam", "D-02: fonts", cwd=folder)
    assert "on feature/redesign" in run(o.repos.publish("carstage"))       # existing key: not blocked
    (folder / "new.ts").write_text("const k = 'AIzaSyNEWNEWNEWNEWNEWNEWNEWNEWNEWNEW123';\n")
    sh("git", "add", "-A", cwd=folder)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "oops", cwd=folder)
    assert run(o.repos.publish("carstage")).startswith("⛔ Not pushed")      # a new key: blocked
