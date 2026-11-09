"""Multi-project workspace: master folder, migration, cross-project builds, feature-branch publishing."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import orchestrator as orch_mod
from conftest import KIT, Recorder, fake_claude, run


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_all(folder, msg):
    sh("git", "add", "-A", cwd=folder)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", msg, cwd=folder)


BOARD = ("| ID | Task | Owner | Status | Depends on |\n|---|---|---|---|---|\n"
         "| {id} | {title} | frontend-dev | {status} | |\n")


def add_project(o, name, status="todo", verify="green"):
    folder = o.cfg.master / "projects" / name
    (folder / "docs").mkdir(parents=True, exist_ok=True)
    (folder / "scripts").mkdir(exist_ok=True)
    (folder / "docs" / "PROGRESS.md").write_text(BOARD.format(id="C-1", title=f"{name} task", status=status))
    (folder / "docs" / "SPEC.md").write_text("### F1. Thing\n- [ ] works\n")
    script = 'echo "::: unit: OK"; echo "VERIFY: PASS"' if verify == "green" else \
        'echo "::: unit: FAILED"; echo "VERIFY: FAIL"; exit 1'
    (folder / "scripts" / "verify.sh").write_text(script)
    if not (folder / ".git").exists():
        sh("git", "init", "-q", "-b", "main", cwd=folder)
    commit_all(folder, "init")
    assert o.set_active_project(name, f"projects/{name}", name.capitalize()).startswith("OK")
    o.set_active_project(o.main_name)
    return folder


def test_fresh_multi_install_has_master_and_main_project(make_orch):
    o = make_orch(multi=True)
    m = o.cfg.master
    assert (m / "CLAUDE.md").read_text().startswith("# Team memory")
    assert (m / ".claude" / "agents" / "frontend-dev.md").exists()
    assert (m / ".claude" / "skills").is_dir() and (m / "docs" / "LESSONS.md").exists()
    assert (m / ".git").exists() and "/projects/" in (m / ".gitignore").read_text()
    assert (o.cfg.workspace / ".git").exists() and (o.cfg.workspace / "docs" / "PROGRESS.md").exists()
    assert o.state.projects["app"]["path"] == "projects/app" and o.state.projects["app"]["mode"] == "main"
    opts = o._options()
    assert opts.cwd == str(m)
    assert "projects/app/docs/PROGRESS.md" in opts.system_prompt["append"]


def test_old_layout_is_migrated_once(base, monkeypatch):
    # an old single-project install: the main repo AT /workspace, CarStage nested inside it
    master = base / "master"
    master.mkdir()
    subprocess.run(["cp", "-r", f"{KIT}/workspace-template/.", str(master)], check=True)
    sh("git", "init", "-q", "-b", "main", cwd=master)
    (master / "docs" / "LESSONS.md").write_text("# Lessons\n- L-1 keep this lesson\n")
    commit_all(master, "stocfresh history")
    (master / "carstage" / "docs").mkdir(parents=True)
    (master / "carstage" / "docs" / "PROGRESS.md").write_text("x")
    data = base / "data"
    data.mkdir()
    (data / "state.json").write_text(json.dumps({"projects": {"carstage": {"path": "carstage", "title": "CarStage"}},
                                                 "active_project": "carstage"}))
    cfg = orch_mod.Config(workspace=master / "projects" / "stocfresh", root=master, data_dir=data,
                          template_dir=KIT / "workspace-template", pm_prompt_file=KIT / "bot/prompts/pm.md",
                          studio_dir=base / "studio", studio_template=KIT / "studio-template",
                          curator_worktree=base / "cwt", github_repo="vlad/stocfresh")
    o = orch_mod.Orchestrator(cfg, Recorder())
    main = master / "projects" / "stocfresh"
    assert sh("git", "log", "--format=%s", "-1", cwd=main) == "stocfresh history"     # history kept
    assert (master / "projects" / "carstage" / "docs" / "PROGRESS.md").exists()
    assert o.state.projects["carstage"]["path"] == "projects/carstage"
    assert "L-1 keep this lesson" in (master / "docs" / "LESSONS.md").read_text()      # lessons carried over
    assert (master / ".git").exists() and not (master / "docs" / "SPEC.md").exists()
    assert any("multi-project" in m for m in o.state.inbox)
    assert o.project_root("carstage")[1] == master / "projects" / "carstage"
    # a second start doesn't move anything again
    o2 = orch_mod.Orchestrator(cfg, Recorder())
    assert o2.state.projects["carstage"]["path"] == "projects/carstage"


def test_build_covers_projects_with_open_tasks_and_verifies_what_changed(make_orch, monkeypatch):
    o = make_orch(multi=True, max_rounds=2)
    (o.cfg.workspace / "docs" / "PROGRESS.md").write_text(BOARD.format(id="T-1", title="main", status="done"))
    (o.cfg.workspace / "scripts" / "verify.sh").write_text('echo "VERIFY: PASS"')
    cs = add_project(o, "carstage", verify="red")
    assert o.resolve_scope() == ["carstage"]                    # only projects with open tasks
    assert o.resolve_scope(["app", "carstage"]) == ["app", "carstage"]

    def round1(prompt, options):
        (cs / "editor.ts").write_text("x")
        commit_all(cs, "D-04: editor")
        return [orch_mod.SystemMessage(subtype="init", data={"session_id": "s"}),
                __import__("conftest").result("did D-04")]
    fc = fake_claude(monkeypatch, round1, "fixed it\nNEEDS_INPUT: which colour?")
    async def go():
        assert o.start_build()
        await o.build_task
    run(go())
    first, second = fc.calls[0][0], fc.calls[1][0]
    assert "projects/carstage/docs/PROGRESS.md" in first
    assert "verify run for carstage after the last round FAILED" in second
    assert o.rec.last("Round 1 done").startswith("Round 1 done · ❌ carstage")
    assert "no scripts" not in o.state.projects["carstage"]["last_verify"]
    st = o.status(project="carstage")
    assert "❌ failing" in st and "📁 App" in st


def test_pm_reply_starts_the_build_loop(make_orch, monkeypatch):
    o = make_orch(multi=True)
    add_project(o, "carstage")
    fake_claude(monkeypatch, "Starting D-04 and D-05 in CarStage, ~1 h.\nSTART_BUILD: carstage")
    started = []
    monkeypatch.setattr(o, "start_build", lambda scope=None: started.append(scope) or True)
    reply = run(o.chat("go ahead with the plan"))
    assert started == [["carstage"]]
    assert "START_BUILD" not in reply and "Build started" in reply


def test_feature_branch_is_published_as_a_pull_request(make_orch, tmp_path, monkeypatch):
    bare = tmp_path / "gh" / "vlad" / "carstage.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    sh("git", "clone", "-q", str(bare), str(seed))
    (seed / "README.md").write_text("hi\n")
    commit_all(seed, "init")
    sh("git", "push", "-q", "origin", "main", cwd=seed)
    monkeypatch.setenv("PROJECTS_GIT_URL", f"file://{tmp_path}/gh/{{repo}}.git")
    o = make_orch(multi=True)
    o.cfg.github_token = "t"
    assert "linked" in run(o.repos.link("carstage", "vlad/carstage"))
    folder = o.cfg.master / "projects" / "carstage"
    assert (folder / "README.md").exists()
    sh("git", "switch", "-q", "-c", "feature/D-04-editor", cwd=folder)
    (folder / "editor.ts").write_text("x")
    commit_all(folder, "D-04: editor")
    out = run(o.repos.publish("carstage"))
    assert "on feature/D-04-editor" in out and "pull/7" in out
    assert sh("git", "--git-dir", str(bare), "log", "--format=%s", "-1", "feature/D-04-editor") == "D-04: editor"
    assert sh("git", "--git-dir", str(bare), "log", "--format=%s", "-1", "main") == "init"
    assert "carstage" in o.projects_brief() and "pull" in o.projects_brief()


def test_scorecard_counts_rounds_tasks_and_usage_per_project(make_orch, monkeypatch):
    import score
    from conftest import result
    o = make_orch(multi=True, max_rounds=1)
    cs = add_project(o, "carstage")
    (cs / "a.ts").write_text("x")
    commit_all(cs, "D-04: editor")
    (cs / "b.ts").write_text("y")
    commit_all(cs, "D-05: plans page")
    msg = result("done")
    msg.usage = {"input_tokens": 1000, "output_tokens": 500}
    msg.total_cost_usd = 0.42
    msg.duration_ms = 120000
    score.record_turn(o, ["carstage", "app"], msg)
    score.add(o, "carstage", rounds=2, red=1)
    rep = score.report(o, 7)
    assert "Carstage" in rep or "carstage" in rep.lower()
    assert "2 task(s) committed" in rep and "2 build round(s), 50% green" in rep
    assert "≈$0.21" in rep and "1500" not in rep


def test_deploys_work_per_project(make_orch, monkeypatch):
    o = make_orch(multi=True, pipeline_timeout_s=1)
    add_project(o, "carstage")
    text, can = run(o.deploy_preview("carstage"))
    assert "isn't connected to GitHub" in text and not can
    o.state.projects["carstage"]["repo"] = "vlad/carstage"
    text, can = run(o.deploy_preview("carstage"))
    assert "no staging address" in text and "/project urls carstage" in text
    o.state.projects["carstage"].update(staging_url="https://stg.carstage.ro", production_url="https://carstage.ro")
    monkeypatch.setattr(o, "running_version", lambda url: _ver(url))
    text, can = run(o.deploy_preview("carstage"))
    assert can and "Carstage: Production will get" in text and "abc1234" in text
    monkeypatch.setattr(o, "_watch", lambda coro: coro.close())
    msg = run(o.deploy_production("carstage"))
    assert msg.startswith("🚀 Carstage: Production deploy started")
    log = (o.cfg.data_dir.parent / "gh.log").read_text()
    assert "workflow run deploy-production.yml -R vlad/carstage" in log


async def _ver(url):
    return {"https://stg.carstage.ro": "abc1234def", "https://carstage.ro": "0001111"}.get(url)


def _old_install(base, name="stocfresh"):
    master = base / "master"
    master.mkdir()
    subprocess.run(["cp", "-r", f"{KIT}/workspace-template/.", str(master)], check=True)
    sh("git", "init", "-q", "-b", "main", cwd=master)
    commit_all(master, "history")
    (master / "carstage" / "docs").mkdir(parents=True)
    (master / "carstage" / "docs" / "PROGRESS.md").write_text("x")
    data = base / "data"
    data.mkdir()
    (data / "state.json").write_text(json.dumps({"projects": {"carstage": {"path": "carstage"}}}))
    cfg = orch_mod.Config(workspace=master / "projects" / name, root=master, data_dir=data,
                          template_dir=KIT / "workspace-template", pm_prompt_file=KIT / "bot/prompts/pm.md",
                          studio_dir=base / "studio", studio_template=KIT / "studio-template",
                          curator_worktree=base / "cwt", github_repo=f"vlad/{name}")
    return master, cfg


def test_migration_never_runs_twice_even_if_the_repo_name_changes(base):
    master, cfg = _old_install(base)
    o = orch_mod.Orchestrator(cfg, Recorder())
    assert sh("git", "log", "--format=%s", "-1", cwd=master / "projects" / "stocfresh") == "history"
    # later the owner changes GITHUB_REPO: the main project must stay where it is
    cfg2 = orch_mod.Config(**{**cfg.__dict__, "workspace": master / "projects" / "shop", "github_repo": "vlad/shop"})
    o2 = orch_mod.Orchestrator(cfg2, Recorder())
    assert o2.cfg.workspace == master / "projects" / "stocfresh"
    assert not (master / "projects" / "shop").exists()
    assert (master / "CLAUDE.md").read_text().startswith("# Team memory")      # team repo untouched


def test_interrupted_migration_resumes(base, monkeypatch):
    import layout
    master, cfg = _old_install(base)
    real_move, calls = layout.shutil.move, []

    def flaky(src, dst):
        calls.append(src)
        if src.endswith("/docs") and len(calls) < 50:
            raise OSError("simulated crash in the middle")
        return real_move(src, dst)
    monkeypatch.setattr(layout.shutil, "move", flaky)
    import pytest
    with pytest.raises(OSError):
        orch_mod.Orchestrator(cfg, Recorder())
    assert not (master / "projects" / "stocfresh").exists()        # not live yet: nothing half-used
    monkeypatch.setattr(layout.shutil, "move", real_move)
    o = orch_mod.Orchestrator(cfg, Recorder())                     # next start finishes the same plan
    main = master / "projects" / "stocfresh"
    assert (main / "docs" / "SPEC.md").exists() and sh("git", "log", "--format=%s", "-1", cwd=main) == "history"
    assert o.state.projects["carstage"]["path"] == "projects/carstage"
    assert not (master / "projects" / ".migrating-stocfresh").exists()
