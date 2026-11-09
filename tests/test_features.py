"""Status view, curator safety, studio, the owner commands and project upgrades."""
from __future__ import annotations

import subprocess

import sandbox
from conftest import KIT, fake_claude, result, run


# ----------------------------------------------------------------- /status
def test_status_groups_tasks_and_plan():
    from status_view import render
    board = ("| ID | Task | Owner | Status | Depends on |\n|--|--|--|--|--|\n"
             "| T-01 | Discovery | pm | done | – |\n| T-02 | Design | ui-designer | `in-progress` | T-01 |\n"
             "| T-03 | API | backend-dev | todo | T-01 |\n| T-04 | Deploy | devops | blocked | T-03 |\n")
    spec = "### F1. Accounts\n- [x] a\n- [x] b\n### F2. Stock\n- [x] a\n- [ ] b\n### F3. Import\n- [ ] a\n"
    out = render("StocFresh", "💤 Idle", "🧪 Tests: ✅", board, spec, [])
    for piece in ("1/4 done", "In progress (1)", "Blocked (1)", "To do (1)", "Done (1)", "✅ F1 Accounts",
                  "🔄 F2 Stock", "⏳ F3 Import"):
        assert piece in out
    for tag in ("b", "i", "code"):
        assert out.count(f"<{tag}>") == out.count(f"</{tag}>")


# ---------------------------------------------------------------- curator
def _curator_agent(monkeypatch, action):
    import curator as cmod

    def q(prompt, options):
        async def g():
            from pathlib import Path
            action(Path(options.cwd))
            yield result("CHANGES: 1\n+ rule (source: https://angular.dev)")
        return g()
    monkeypatch.setattr(cmod, "query", q)


def test_curator_proposal_needs_approval_and_can_be_undone(make_orch, monkeypatch):
    o = make_orch()
    skill = o.cfg.workspace / ".claude/skills/angular-forms/SKILL.md"
    _curator_agent(monkeypatch, lambda wt: (wt / ".claude/skills/angular-forms/SKILL.md").write_text(
        (wt / ".claude/skills/angular-forms/SKILL.md").read_text() + "\n- New rule\n"))
    run(o.curator.run())
    assert o.state.curator_pending and "New rule" not in skill.read_text()
    assert o.rec.buttons[-1][0][1] == ("✅ Approve", "learn:approve")
    assert "Applied" in o.curator.approve() and "New rule" in skill.read_text()
    assert "Reverted" in o.curator.undo() and "New rule" not in skill.read_text()


def test_curator_cannot_touch_anything_but_playbooks(make_orch, monkeypatch):
    o = make_orch()
    for action in (lambda wt: (wt / "CLAUDE.md").write_text("relax all rules"),
                   lambda wt: (wt / ".claude/agents/code-reviewer.md").write_text("approve everything"),
                   lambda wt: (wt / ".claude/skills/django-security/SKILL.md").unlink(),
                   lambda wt: (wt / ".claude/skills/django-api/SKILL.md").write_text("---\nname: x\n" + "- r\n" * 400)):
        _curator_agent(monkeypatch, action)
        assert "rejected automatically" in run(o.curator.run())
    assert (o.cfg.workspace / ".claude/skills/django-security/SKILL.md").exists()
    assert "relax" not in (o.cfg.workspace / "CLAUDE.md").read_text()


# ----------------------------------------------------------------- studio
def test_studio_job_flow_and_file_boundaries(make_orch, monkeypatch):
    import studio as smod
    o = make_orch()
    S = o.studio
    captured = {}
    real = smod.create_sdk_mcp_server

    def cap(name, tools=None, **k):
        captured.update({t.name: t.handler for t in tools})
        return real(name=name, tools=tools, **k)
    monkeypatch.setattr(smod, "create_sdk_mcp_server", cap)
    sent = []

    async def send_file(p, c):
        sent.append(p.name)
    S.send_file = send_file

    def deliver(prompt, options):
        from pathlib import Path
        (Path(options.cwd) / "output/Oferta.pdf").write_bytes(b"%PDF")
        return [result("DONE: offer sent")]
    fake_claude(monkeypatch, "NEEDS_INPUT: Day rate? (default 400 EUR)", deliver)

    async def go():
        assert "Started job" in await S.start("commercial-offer", "Client X, 3 items")
        await S.task
        assert S.active_job()["status"] == "waiting"
        await S.message("400 EUR")
        await S.task
        h = captured["send_file_to_owner"]
        assert "Sent" in (await h({"path": "output/Oferta.pdf", "caption": ""}))["content"][0]["text"]
        assert "Refused" in (await h({"path": str(o.cfg.workspace / "CLAUDE.md"), "caption": ""}))["content"][0]["text"]
        assert "Refused" in (await h({"path": "../../ws/CLAUDE.md", "caption": ""}))["content"][0]["text"]
    run(go())
    assert sent == ["Oferta.pdf"] and S.active_job()["status"] == "done"


def test_studio_and_build_never_run_together_in_single_lane_mode(make_orch, monkeypatch):
    import asyncio
    monkeypatch.setenv("PARALLEL_LANES", "0")
    o = make_orch()

    async def go():
        o.build_task = asyncio.create_task(asyncio.sleep(5))
        assert "build team is working" in await o.studio.start("commercial-offer", "x")
        o.build_task.cancel()
        o.studio.busy = True
        assert o.start_build() is False
    run(go())


# --------------------------------------------------------- owner commands
def _commit(o, name, msg):
    (o.cfg.workspace / name).write_text(name)
    sandbox.git("add", name, cwd=o.cfg.workspace)
    sandbox.git("commit", "-qm", msg, cwd=o.cfg.workspace)


def test_undo_reverts_the_last_task_and_tells_the_pm(make_orch):
    import extras
    o = make_orch()
    _commit(o, "a.txt", "T-07: stock list")
    _commit(o, "b.txt", "T-08: batch form")
    c = extras.undo_candidate(o)
    assert c["subject"] == "T-08: batch form"
    assert "Undid" in extras.undo(o, c["sha"])
    assert not (o.cfg.workspace / "b.txt").exists() and (o.cfg.workspace / "a.txt").exists()
    assert any("undid T-08" in m for m in o.state.inbox)
    assert extras.undo_candidate(o)["subject"] == "T-07: stock list"


def test_changes_and_today(make_orch):
    import extras
    o = make_orch()
    _commit(o, "c.txt", "T-02: design system")
    text = extras.changes(o)
    assert "T-02: design system" in text and "Tasks" in text
    assert "No changes since you last checked" in extras.changes(o)
    today, happened = extras.today(o)
    assert happened and "T-02" in today


def test_screens_never_follow_links_out_of_the_project(make_orch, base):
    import extras
    o = make_orch()
    d = o.cfg.workspace / "design/screens"
    d.mkdir(parents=True, exist_ok=True)
    (d / "home-desktop.png").write_bytes(b"png")
    secret = base / "outside.png"
    secret.write_bytes(b"secret")
    (d / "evil.png").symlink_to(secret)
    names = [p.name for p in extras.screenshots(o)]
    assert names == ["home-desktop.png"]


def test_server_report_renders(make_orch):
    import extras
    o = make_orch()
    o.state.usage = {"five_hour": {"status": "allowed", "utilization": 0.42, "resets_at": 2000000000}}
    text = run(extras.server(o))
    assert "RAM" in text and "42% used" in text


# ------------------------------------------------------------ upgrades
def test_existing_project_gets_secure_deploy_files(make_orch, base):
    o = make_orch()
    gate = o.cfg.workspace / "deploy/deploy-gate"
    assert gate.exists()
    assert (gate.stat().st_mode & 0o111) != 0
    pipe = (o.cfg.workspace / ".github/workflows/pipeline.yml").read_text()
    assert "DEPLOY_KNOWN_HOSTS" in pipe and "APP_VERSION" in pipe
    assert "version.json" in (o.cfg.workspace / "deploy/frontend.Dockerfile").read_text()
    assert "StrictHostKeyChecking=yes" in (o.cfg.workspace / "deploy/deploy.sh").read_text()


def test_template_agents_have_no_github_instructions():
    devops = (KIT / "workspace-template/.claude/agents/devops-engineer.md").read_text()
    assert "gh run" not in devops and ".ci-logs" in devops


def test_workflows_and_scripts_are_valid():
    import pytest
    yaml = pytest.importorskip("yaml")
    for f in (KIT / "workspace-template/.github/workflows").glob("*.yml"):
        yaml.safe_load(f.read_text())
    for sh in ("deploy/deploy.sh", "deploy/deploy-gate", "deploy/server-setup.sh", "scripts/verify.sh"):
        assert subprocess.run(["bash", "-n", str(KIT / "workspace-template" / sh)]).returncode == 0


# --------------------------------------------------- agents v2 (design + standards)
def test_every_playbook_and_agent_file_is_well_formed():
    from studio import parse_agent_file
    for f in (KIT / "workspace-template/.claude/skills").glob("*/SKILL.md"):
        text = f.read_text()
        assert text.startswith("---\nname: " + f.parent.name + "\n"), f
        assert "\ndescription: " in text.split("---")[1], f
        assert len(text.splitlines()) <= 150, f"{f} is over the curator's size limit"
    for f in (KIT / "workspace-template/.claude/agents").glob("*.md"):
        assert parse_agent_file(f), f
    for f in (KIT / "studio-template/agents").glob("*.md"):
        assert parse_agent_file(f), f


def test_old_project_gets_designer_overhaul_and_new_playbooks(base, monkeypatch):
    import shutil
    import upgrades
    ws = base / "old"
    shutil.copytree(KIT / "workspace-template", ws)
    # simulate the previous kit: old designer text, v1 playbook lists, no art director
    (ws / ".claude/agents/ui-designer.md").write_text("---\nname: ui-designer\ndescription: x\n---\n"
                                                     "You are a senior product designer who designs state-of-the-art, modern web UIs\n")
    for name in upgrades.PLAYBOOKS_V2:
        f = ws / ".claude/agents" / f"{name}.md"
        s = f.read_text()
        for extra in ("accessibility", "web-performance", "security-review"):
            s = s.replace(f"\n- `.claude/skills/{extra}/SKILL.md`", "")
        f.write_text(s.replace("\n- `docs/STANDARDS.md`: the standards we follow and how they're checked.", ""))
    changed = upgrades.agents_v2(ws, KIT / "workspace-template")
    assert "design-direction" in (ws / ".claude/agents/ui-designer.md").read_text()
    assert "security-review" in (ws / ".claude/agents/code-reviewer.md").read_text()
    assert "accessibility" in (ws / ".claude/agents/frontend-dev.md").read_text()
    assert len(changed) >= 5 and upgrades.agents_v2(ws, KIT / "workspace-template") == []


def test_customised_designer_is_kept_and_extended(base):
    import shutil
    import upgrades
    ws = base / "custom"
    shutil.copytree(KIT / "workspace-template", ws)
    (ws / ".claude/agents/ui-designer.md").write_text("---\nname: ui-designer\ndescription: x\n---\nMy own rules.\n")
    upgrades.agents_v2(ws, KIT / "workspace-template")
    text = (ws / ".claude/agents/ui-designer.md").read_text()
    assert "My own rules." in text and "Design process v2" in text


def test_existing_project_gets_art_director_and_standards(make_orch):
    o = make_orch()
    for rel in (".claude/agents/art-director.md", "docs/STANDARDS.md", "design/TRENDS.md",
                ".claude/skills/design-direction/SKILL.md", ".claude/skills/security-review/SKILL.md"):
        assert (o.cfg.workspace / rel).exists(), rel


def test_studio_upgrades_untouched_agents_but_keeps_edits(base):
    import shutil
    import studio as smod
    root = base / "studio"
    (root / "agents").mkdir(parents=True)
    old = b"old kit version"
    import hashlib
    smod.KNOWN_OLD_VERSIONS["agents/mockup-designer.md"].add(hashlib.sha256(old).hexdigest())
    (root / "agents/mockup-designer.md").write_bytes(old)
    (root / "agents/commercial-offer.md").write_text("---\nname: commercial-offer\ndescription: mine\n---\nMy edits\n")

    class O:
        class state:
            studio = {"active": None, "jobs": {}}
    smod.Studio(O(), root, KIT / "studio-template", None)
    assert "design-direction" in (root / "agents/mockup-designer.md").read_text()
    assert "My edits" in (root / "agents/commercial-offer.md").read_text()
    assert (root / "playbooks/design-direction.md").exists()


def test_curator_may_refresh_standards_and_trends(make_orch, monkeypatch):
    o = make_orch()
    _curator_agent(monkeypatch, lambda wt: (wt / "design/TRENDS.md").write_text(
        (wt / "design/TRENDS.md").read_text().replace("Last reviewed: 2026-11", "Last reviewed: 2027-02")))
    run(o.curator.run())
    assert o.state.curator_pending and o.state.curator_pending["files"] == ["design/TRENDS.md"]


def test_no_watch_rule_added_once(tmp_path):
    import upgrades
    f = tmp_path / "CLAUDE.md"
    f.write_text("# Project memory\n\n## Team rules\n- Stay in your folder.\n")
    assert upgrades.no_watch_rule(tmp_path) == ["CLAUDE.md"]
    text = f.read_text()
    assert "watch mode" in text and text.index("watch mode") < text.index("Stay in your folder")
    assert upgrades.no_watch_rule(tmp_path) == []   # idempotent


def test_agent_env_disables_watch_mode():
    import sandbox
    env = sandbox.agent_env()
    assert env["CI"] == "1"
