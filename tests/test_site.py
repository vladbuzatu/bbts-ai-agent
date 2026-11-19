"""Preview site: the compose safety check (ops/site_check.py) and the bot's side (bot/preview_site.py).
The real deploy (ops/site.sh with Docker and Caddy) is tested by tests/site_e2e.sh in CI."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time

import pytest

import egress
import hostctl
import layout
import orchestrator as orch_mod
import preview_site
from conftest import KIT, run

spec = importlib.util.spec_from_file_location("site_check", KIT / "ops" / "site_check.py")
site_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site_check)

SRC = "/var/lib/bbts/site/app/src-1"


def svc(**kw):
    return {"command": None, "entrypoint": None, "networks": {"default": None}, **kw}


def cfg(services, **top):
    return {"name": "site-app", "networks": {"default": {"name": "site-app_default", "ipam": {}}},
            "services": services, **top}


def good():
    return cfg({
        "db": svc(image="postgres:17-alpine", volumes=[{"type": "volume", "source": "db", "target": "/d", "volume": {}}]),
        "backend": svc(build={"context": SRC, "dockerfile": "deploy/backend.Dockerfile"},
                       environment={"X": "1"}, env_file=[{"path": f"{SRC}/backend/.env.preview", "required": True}]),
        "web": svc(build={"context": SRC, "dockerfile": "deploy/frontend.Dockerfile"},
                   tmpfs=["/tmp"], volumes=[{"type": "tmpfs", "target": "/cache"}]),
    }, volumes={"db": {"name": "site-app_db"}})


def test_a_normal_django_angular_compose_passes():
    r = site_check.check(good(), SRC)
    assert r["ok"], r["errors"]
    assert r["built"] == ["backend", "web"] and r["port"] == 80


@pytest.mark.parametrize("change, expected", [
    (lambda c: c["services"]["web"].update(privileged=True), "'privileged' isn't allowed"),
    (lambda c: c["services"]["web"].update(ports=[{"target": 22, "published": "2222"}]), "'ports' isn't allowed"),
    (lambda c: c["services"]["web"].update(network_mode="host"), "'network_mode'"),
    (lambda c: c["services"]["web"].update(cap_add=["SYS_ADMIN"]), "'cap_add'"),
    (lambda c: c["services"]["web"].update(pid="host"), "'pid'"),
    (lambda c: c["services"]["web"].update(devices=["/dev/sda"]), "'devices'"),
    (lambda c: c["services"]["web"].update(volumes_from=["x"]), "'volumes_from'"),
    (lambda c: c["services"]["web"].update(security_opt=["seccomp=unconfined"]), "'security_opt'"),
    (lambda c: c["services"]["web"].update(logging={"driver": "json-file"}), "'logging'"),
    (lambda c: c["services"]["web"].update(provider={"type": "x"}), "'provider'"),
    (lambda c: c["services"]["web"].update(
        volumes=[{"type": "bind", "source": "/", "target": "/host"}]), "bind mount of /"),
    (lambda c: c["services"]["web"].update(
        volumes=[{"type": "volume", "source": "nope", "target": "/x"}]), "must be declared"),
    (lambda c: c["services"]["backend"].update(env_file=[{"path": "/root/.env"}]), "env_file /root/.env is outside"),
    (lambda c: c["services"]["backend"].update(
        env_file=[{"path": f"{SRC}/../../app/.env"}]), "is outside the project"),
    (lambda c: c["services"]["backend"]["build"].update(context="/etc"), "build context /etc"),
    (lambda c: c["services"]["backend"]["build"].update(context="https://github.com/x/y.git"), "build context"),
    (lambda c: c["services"]["backend"]["build"].update(dockerfile="../../../../etc/Dockerfile"), "dockerfile"),
    (lambda c: c["services"]["backend"]["build"].update(network="host"), "build 'network'"),
    (lambda c: c["services"]["backend"]["build"].update(secrets=["s"]), "build 'secrets'"),
    (lambda c: c["services"]["backend"]["build"].update(additional_contexts={"x": "/"}), "additional_contexts"),
    (lambda c: c["volumes"]["db"].update(driver_opts={"type": "none", "o": "bind", "device": "/"}), "driver_opts"),
    (lambda c: c["volumes"]["db"].update(external=True), "'external'"),
    (lambda c: c["networks"].update(evil={"driver": "macvlan"}), "network 'evil'"),
    (lambda c: c.update(secrets={"s": {"file": "/etc/shadow"}}), "top-level 'secrets'"),
    (lambda c: c.update(configs={"s": {"file": "/etc/shadow"}}), "top-level 'configs'"),
    (lambda c: c["services"].pop("web"), "service called 'web'"),
    (lambda c: c["services"]["web"].update(labels={"bbts.site.port": "x"}), "must be a port number"),
    (lambda c: c["services"]["db"].pop("image"), "needs build or image"),
])
def test_anything_that_reaches_the_host_is_refused(change, expected):
    c = good()
    change(c)
    r = site_check.check(c, SRC)
    assert not r["ok"]
    assert any(expected in e for e in r["errors"]), r["errors"]


def test_symlinked_folder_out_of_the_project_is_caught(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "escape").symlink_to("/etc")
    c = good()
    c["services"]["backend"]["build"]["context"] = str(src / "escape")
    c["services"]["web"]["build"]["context"] = str(src)
    c["services"]["backend"]["env_file"] = []
    r = site_check.check(c, str(src))
    assert any("build context" in e for e in r["errors"])


def test_web_port_from_label():
    c = good()
    c["services"]["web"]["labels"] = {"bbts.site.port": "8000"}
    assert site_check.check(c, SRC)["port"] == 8000


def test_override_limits_every_service_and_exposes_only_web():
    c = good()
    c["services"]["backend"]["mem_limit"] = "2g"
    c["services"]["db"]["mem_limit"] = "128m"
    o = site_check.override(c, "app", "123-abc", "site-app", "512m")
    s = o["services"]
    assert s["backend"]["mem_limit"] == 512 * 1024 ** 2          # capped
    assert s["db"]["mem_limit"] == 128 * 1024 ** 2               # its own, smaller
    assert all(v["pids_limit"] == 512 and v["cpus"] == 1.0 for v in s.values())
    assert s["web"]["image"] == "bbts-site-app-web:123-abc" and "image" not in s["db"]
    assert s["web"]["networks"]["bbts-proxy"] == {"aliases": ["site-app"]}
    assert "networks" not in s["backend"]
    assert o["networks"]["bbts-proxy"] == {"name": "proxy", "external": True}


def test_cli_reads_compose_json():
    r = subprocess.run(["python3", str(KIT / "ops" / "site_check.py"), "check", SRC],
                       input=json.dumps(good()), capture_output=True, text=True, check=True)
    assert json.loads(r.stdout)["ok"]


# ---------------------------------------------------------------- the bot's side
@pytest.fixture
def site(tmp_path, monkeypatch):
    control = tmp_path / "control"
    control.mkdir()
    (control / "site-ready").write_text("1")
    monkeypatch.setattr(hostctl, "CONTROL", control)
    monkeypatch.setenv("SITE_DOMAIN", "bbts-preview.duckdns.org")
    repo = tmp_path / "proj"
    (repo / "preview").mkdir(parents=True)
    (repo / "preview" / "compose.yml").write_text("services:\n  web:\n    image: caddy\n")
    (repo / "README").write_text("hi\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "Add home page"], check=True)
    return control, repo


def test_not_set_up_says_what_to_do(site, monkeypatch):
    control, repo = site
    monkeypatch.setenv("SITE_DOMAIN", "")
    text, ident = preview_site.request_deploy("tstauto", repo)
    assert ident is None and "SITE_DOMAIN" in text
    monkeypatch.setenv("SITE_DOMAIN", "x.duckdns.org")
    (control / "site-ready").unlink()
    text, ident = preview_site.request_deploy("tstauto", repo)
    assert ident is None and "ops/install.sh" in text


def test_deploy_snapshots_committed_code_and_writes_request(site):
    control, repo = site
    (repo / "README").write_text("uncommitted\n")
    text, ident = preview_site.request_deploy("tstauto", repo)
    assert ident and "https://bbts-preview.duckdns.org" in text and "Uncommitted changes" in text
    req = json.loads((control / "site-request").read_text())
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    assert req == {"id": ident, "action": "deploy", "project": "tstauto", "commit": head, "title": "Add home page"}
    files = subprocess.run(["tar", "-tf", str(control / "site" / f"{ident}.tar")], capture_output=True, text=True).stdout
    assert "preview/compose.yml" in files and "README" in files
    tar_readme = subprocess.run(["tar", "-xOf", str(control / "site" / f"{ident}.tar"), "README"],
                                capture_output=True, text=True).stdout
    assert tar_readme == "hi\n"                                   # committed version, not the working copy
    assert preview_site.read_status()["state"] == "queued"
    # a second request while one is pending is refused
    text2, ident2 = preview_site.request_deploy("tstauto", repo)
    assert ident2 is None and "waiting" in text2


def test_deploy_needs_preview_compose(site):
    control, repo = site
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "preview/compose.yml"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "rm"], check=True)
    text, ident = preview_site.request_deploy("tstauto", repo)
    assert ident is None and "preview/compose.yml" in text and not (control / "site-request").exists()


def test_bad_project_names_are_refused(site):
    _, repo = site
    for name in ("../x", "Tst Auto", "a" * 40, ""):
        assert preview_site.request_deploy(name, repo)[1] is None


def test_busy_while_building_but_not_forever(site):
    control, repo = site
    (control / "site-status.json").write_text(json.dumps({"state": "building", "at": time.time(), "message": "Building x"}))
    assert "busy" in preview_site.request_deploy("tstauto", repo)[0]
    (control / "site-status.json").write_text(json.dumps({"state": "building", "at": time.time() - 3 * 3600}))
    assert preview_site.request_deploy("tstauto", repo)[1]        # a stale status doesn't block forever


def test_wait_returns_the_result_for_this_request(site):
    control, repo = site
    _, ident = preview_site.request_deploy("tstauto", repo)

    async def host_finishes():
        import asyncio
        await asyncio.sleep(0.1)
        (control / "site-request").unlink()
        (control / "site-status.json").write_text(json.dumps(
            {"state": "ok", "id": ident, "project": "tstauto", "commit": "abc1234", "url": "https://x"}))

    async def both():
        import asyncio
        _, s = await asyncio.gather(host_finishes(), preview_site.wait(ident, 5, poll=0.05))
        return s

    s = run(both())
    assert s["state"] == "ok"
    assert "is live at https://x" in preview_site.result_text(s)


def test_failed_result_includes_the_log(site):
    control, _ = site
    (control / "site-log.txt").write_text("step 1\nERROR: npm ci failed\n")
    text = preview_site.result_text({"state": "failed", "message": "the build failed"})
    assert "the build failed" in text and "npm ci failed" in text


def test_stop_and_summary(site):
    control, _ = site
    (control / "site-status.json").write_text(json.dumps(
        {"state": "ok", "project": "tstauto", "commit": "abcdef1234", "title": "Home", "at": time.time()}))
    assert "Live: tstauto abcdef1" in preview_site.summary()
    assert "Stopping" in preview_site.request_stop()
    assert json.loads((control / "site-request").read_text())["action"] == "stop"


def test_pm_gets_site_tools_only_when_configured(make_orch, monkeypatch, tmp_path):
    monkeypatch.setattr(hostctl, "CONTROL", tmp_path)
    calls = []
    real = preview_site.make_tools
    monkeypatch.setattr(preview_site, "make_tools", lambda o: calls.append(o) or real(o))
    monkeypatch.delenv("SITE_DOMAIN", raising=False)
    o = make_orch()
    o._options()
    assert calls == []
    monkeypatch.setenv("SITE_DOMAIN", "x.duckdns.org")
    o._options()
    assert calls == [o]
    assert [t.name for t in real(o)] == ["deploy_preview_site", "preview_site_status"]


def test_deploy_tool_refuses_unknown_project(make_orch, site):
    o = make_orch()
    deploy = preview_site.make_tools(o)[0]
    out = run(deploy.handler({"name": "nope"}))
    assert "Unknown project 'nope'" in out["content"][0]["text"]


# ---------------------------------------------------------------- browser and allowlist
def test_browser_server_uses_installed_chromium(tmp_path, monkeypatch):
    chrome = tmp_path / "chromium-1254" / "chrome-linux64" / "chrome"
    chrome.parent.mkdir(parents=True)
    chrome.write_text("")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "playwright-mcp").write_text("#!/bin/sh\n")
    (bin_dir / "playwright-mcp").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    s = orch_mod.browser_server(tmp_path / ".browser")
    assert s["command"] == "playwright-mcp"
    a = s["args"]
    assert a[a.index("--executable-path") + 1] == str(chrome)
    assert "--headless" in a and "--isolated" in a and "--proxy-server" not in a
    monkeypatch.setattr(orch_mod.sandbox, "egress_restricted", lambda: True)
    a = orch_mod.browser_server(tmp_path / ".browser")["args"]
    assert a[a.index("--proxy-server") + 1] == f"http://127.0.0.1:{orch_mod.sandbox.PROXY_PORT}"
    monkeypatch.setenv("BROWSER", "0")
    assert orch_mod.browser_server(tmp_path / ".browser") is None


def test_no_browser_without_chromium(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "none"))
    assert orch_mod.browser_server(tmp_path) is None


def test_site_domain_is_on_the_agents_allowlist(monkeypatch):
    monkeypatch.setenv("SITE_DOMAIN", "https://BBTS-Preview.duckdns.org/")
    assert egress.allowed("bbts-preview.duckdns.org")
    assert not egress.allowed("other.duckdns.org")
    monkeypatch.setenv("SITE_DOMAIN", "")
    assert not egress.allowed("bbts-preview.duckdns.org")


# ---------------------------------------------------------------- existing master folders
def test_old_master_gets_preview_rule_skill_and_gitignore(make_orch):
    o = make_orch(multi=True)
    master = o.cfg.master
    md = master / "CLAUDE.md"
    old_rule = layout.CLAUDE_MD_UPDATES[0][0]
    text = md.read_text().replace(layout.CLAUDE_MD_UPDATES[0][1], old_rule)
    md.write_text(text)
    (master / ".gitignore").write_text("/projects/\n.ci-logs/\n")
    layout.init_master(o)
    assert "deploy_preview_site" in md.read_text() and old_rule not in md.read_text()
    assert ".browser/" in (master / ".gitignore").read_text().splitlines()
    assert (master / ".claude" / "skills" / "preview-site" / "SKILL.md").exists()
    layout.init_master(o)                                         # idempotent
    assert md.read_text().count("deploy_preview_site") == 1


# ---------------------------------------------------------------- deploying a branch
def _commit(repo, msg, **files):
    for name, text in files.items():
        (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _tar_file(control, ident, name):
    return subprocess.run(["tar", "-xOf", str(control / "site" / f"{ident}.tar"), name],
                          capture_output=True, text=True).stdout


def test_deploy_a_local_branch_without_switching(site):
    control, repo = site
    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "feature/seo"], check=True)
    sha = _commit(repo, "SEO meta tags", README="seo\n")
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-"], check=True)      # team is back on main
    text, ident = preview_site.request_deploy("tstauto", repo, ref="feature/seo")
    assert ident and "feature/seo" in text and "from the team's copy" in text
    assert json.loads((control / "site-request").read_text())["commit"] == sha
    assert _tar_file(control, ident, "README") == "seo\n"
    assert "[feature/seo] SEO meta tags" == json.loads((control / "site-request").read_text())["title"]


def test_deploy_a_branch_that_is_only_on_github(site, tmp_path):
    control, repo = site
    origin = tmp_path / "origin"
    subprocess.run(["git", "clone", "-q", str(repo), str(origin)], check=True)
    subprocess.run(["git", "-C", str(origin), "switch", "-qc", "owner-fix"], check=True)
    sha = _commit(origin, "Owner's fix", README="from github\n")
    mirror = tmp_path / "mirror.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(mirror)], check=True)
    subprocess.run(["git", "-C", str(mirror), "fetch", "-q", str(origin), "+refs/heads/*:refs/remotes/origin/*"], check=True)
    text, ident = preview_site.request_deploy("tstauto", repo, ref="owner-fix", github=mirror)
    assert ident and "from GitHub" in text
    assert json.loads((control / "site-request").read_text())["commit"] == sha
    assert _tar_file(control, ident, "README") == "from github\n"


def test_unknown_or_odd_refs_are_refused(site, tmp_path):
    control, repo = site
    text, ident = preview_site.request_deploy("tstauto", repo, ref="nope")
    assert ident is None and "no branch, tag or commit 'nope'" in text
    for bad in ("--upload-pack=x", "a..b", "a b", "x;rm"):
        assert preview_site.request_deploy("tstauto", repo, ref=bad)[1] is None
    assert not (control / "site-request").exists()


def test_branch_without_preview_compose(site):
    control, repo = site
    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "old"], check=True)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "preview/compose.yml"], check=True)
    _commit(repo, "old")
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-"], check=True)
    text, ident = preview_site.request_deploy("tstauto", repo, ref="old")
    assert ident is None and "(old) has no preview/compose.yml" in text


def test_deploy_wrapper_with_github_mirror(make_orch, site, monkeypatch):
    control, _ = site
    o = make_orch(github=True)
    ws = o.cfg.workspace
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "preview").mkdir(exist_ok=True)
    (ws / "preview" / "compose.yml").write_text("services: {}\n")
    _commit(ws, "app")
    # GitHub (the bare remote) has a branch the team doesn't
    gh_work = ws.parent / "gh-work"
    subprocess.run(["git", "clone", "-q", str(ws), str(gh_work)], check=True)
    subprocess.run(["git", "-C", str(gh_work), "switch", "-qc", "hotfix"], check=True)
    sha = _commit(gh_work, "hotfix", README="hot\n")
    subprocess.run(["git", "-C", str(gh_work), "push", "-q", o.cfg.git_remote_url, "hotfix"], check=True)
    if not (o.mirror / "HEAD").exists():
        subprocess.run(["git", "clone", "-q", "--bare", o.cfg.git_remote_url, str(o.mirror)], check=True)
    text, ident = run(preview_site.deploy(o, o.main_name, "hotfix"))
    assert ident, text
    assert "from GitHub" in text and json.loads((control / "site-request").read_text())["commit"] == sha
