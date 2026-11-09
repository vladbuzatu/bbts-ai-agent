"""Pull requests in Telegram: card, merge, change requests, GitHub comments forwarded to the PM."""
from __future__ import annotations

import json
import subprocess

import pytest

from conftest import run


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def linked(make_orch, tmp_path, monkeypatch):
    bare = tmp_path / "gh" / "vlad" / "carstage.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("hi\n")
    sh("git", "add", "-A", cwd=seed)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init", cwd=seed)
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    monkeypatch.setenv("PROJECTS_GIT_URL", f"file://{tmp_path}/gh/{{repo}}.git")
    o = make_orch(multi=True)
    o.cfg.github_token = "t"
    run(o.repos.link("carstage", "vlad/carstage"))
    folder = o.cfg.master / "projects" / "carstage"
    sh("git", "switch", "-q", "-c", "feature/D-04-editor", cwd=folder)
    (folder / "editor.ts").write_text("x")
    sh("git", "add", "-A", cwd=folder)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "D-04: editor", cwd=folder)
    return o, tmp_path


def test_publish_sends_a_card_with_buttons(linked):
    o, tmp = linked
    run(o.repos.publish("carstage"))
    card = o.rec.last("Pull request #7")
    assert "D-04 editor" in card and "+120" in card and "editor.ts" in card
    buttons = o.rec.buttons[o.rec.msgs.index(card)]
    assert ("✅ Merge", "pr:merge:carstage#7") in buttons[0]
    assert o.state.prs["carstage#7"]["branch"] == "feature/D-04-editor"


def test_merge_and_change_request(linked, monkeypatch):
    o, tmp = linked
    run(o.repos.publish("carstage"))
    started = []
    monkeypatch.setattr(o, "start_build", lambda scope=None: started.append(scope) or True)
    assert "What should change" in o.prs.ask_feedback("carstage#7")
    reply = run(o.prs.feedback("Make the before/after slider bigger"))
    assert "posted on #7" in reply and started == [["carstage"]]
    assert any("slider bigger" in m for m in o.state.inbox)
    log = (o.cfg.data_dir.parent / "gh.log").read_text()
    assert "pr comment 7" in log and "bbts-bot" in log
    assert "Merged #7" in run(o.prs.merge("carstage#7"))
    assert "pr merge 7" in (o.cfg.data_dir.parent / "gh.log").read_text()
    assert o.state.prs["carstage#7"]["state"] == "merged"
    assert any("merged pull request #7" in m for m in o.state.inbox)
    assert "isn't open" in run(o.prs.merge("carstage#7"))


def test_github_comments_reach_the_pm(linked, monkeypatch):
    o, _ = linked
    run(o.repos.publish("carstage"))
    activity = {"state": "OPEN", "reviews": [],
                "comments": [{"id": "c1", "author": {"login": "vladbuzatu"}, "body": "Use the red from the logo"},
                             {"id": "c2", "author": {"login": "vladbuzatu"}, "body": "bot note <!-- bbts-bot -->"}]}
    monkeypatch.setenv("FAKE_GH_PR_ACTIVITY", json.dumps(activity))
    run(o.prs.poll(force=True))
    forwarded = [m for m in o.state.inbox if "GitHub comment" in m]
    assert len(forwarded) == 1 and "red from the logo" in forwarded[0]
    run(o.prs.poll(force=True))                                   # not forwarded twice
    assert len([m for m in o.state.inbox if "GitHub comment" in m]) == 1
    monkeypatch.setenv("FAKE_GH_PR_ACTIVITY", json.dumps(dict(activity, state="MERGED")))
    run(o.prs.poll(force=True))
    assert o.state.prs["carstage#7"]["state"] == "merged"
