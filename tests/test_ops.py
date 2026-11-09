"""Host scripts (ops/): update with health check and rollback, update requests from the bot."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import KIT

FAKES = KIT / "tests" / "fakes_ops"


def sh(*args, cwd=None, env=None, check=True):
    return subprocess.run(args, cwd=cwd, env=env, check=check, capture_output=True, text=True)


def commit(repo, msg):
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", msg, cwd=repo)


@pytest.fixture
def kit(tmp_path):
    """A kit clone on the 'server' with a GitHub origin that has one newer commit."""
    origin = tmp_path / "origin.git"
    sh("git", "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "dev"
    sh("git", "clone", "-q", str(origin), str(work))
    shutil.copytree(KIT / "ops", work / "ops")
    (work / "docker-compose.yml").write_text("name: agent-team-kit\n")
    (work / ".gitignore").write_text(".env\n")
    commit(work, "v1")
    sh("git", "push", "-q", "origin", "main", cwd=work)
    server = tmp_path / "server-kit"
    sh("git", "clone", "-q", str(origin), str(server))
    (server / ".env").write_text("TELEGRAM_BOT_TOKEN=\nSECRET=keep-me\n")
    (work / "NEW").write_text("feature\n")
    commit(work, "v2: new feature")
    sh("git", "push", "-q", "origin", "main", cwd=work)
    control = tmp_path / "control"
    env = dict(os.environ, PATH=f"{FAKES}:{os.environ['PATH']}", CONTROL_DIR=str(control),
               ENV_BACKUP=str(tmp_path / "env.bak"), FAKE_DOCKER_LOG=str(tmp_path / "docker.log"))
    return server, control, env, tmp_path


def status(control):
    return json.loads((control / "update-status.json").read_text())


def test_check_reports_newer_version(kit):
    server, control, env, _ = kit
    r = sh("bash", "ops/update.sh", "--check", cwd=server, env=env)
    assert "1 commit(s) behind" in r.stdout
    av = json.loads((control / "update-available.json").read_text())
    assert av["behind"] == 1 and av["latest"] == "v2: new feature"


def test_update_to_healthy_version_keeps_env(kit):
    server, control, env, tmp = kit
    r = sh("bash", "ops/update.sh", cwd=server, env=dict(env, FAKE_DOCKER_HEALTH="healthy"), check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (server / "NEW").exists()
    assert "SECRET=keep-me" in (server / ".env").read_text()
    assert status(control)["state"] == "ok"
    calls = (tmp / "docker.log").read_text()
    assert "tag bbts-agents:latest bbts-agents:rollback" in calls and "compose up -d" in calls
    assert sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=server).stdout.strip() == "main"


def test_unhealthy_version_is_rolled_back(kit, monkeypatch):
    server, control, env, tmp = kit
    monkeypatch.setattr("time.sleep", lambda s: None)
    # don't wait 4 minutes in the test: make the health loop short
    script = (server / "ops" / "update.sh").read_text().replace("seq 1 48", "seq 1 2").replace("sleep 5", "sleep 0")
    (server / "ops" / "update.sh").write_text(script)
    sh("git", "update-index", "--assume-unchanged", "ops/update.sh", cwd=server)
    r = sh("bash", "ops/update.sh", cwd=server, env=dict(env, FAKE_DOCKER_HEALTH="unhealthy"), check=False)
    assert r.returncode == 1
    assert not (server / "NEW").exists()                                  # back on v1
    assert status(control)["state"] == "rolled_back"
    calls = (tmp / "docker.log").read_text()
    assert "tag bbts-agents:rollback bbts-agents:latest" in calls and "up -d --no-build" in calls


def test_failed_build_changes_nothing(kit):
    server, control, env, _ = kit
    r = sh("bash", "ops/update.sh", cwd=server, env=dict(env, FAKE_DOCKER_BUILD="fail"), check=False)
    assert r.returncode == 1 and not (server / "NEW").exists()
    assert status(control)["state"] == "failed"


def test_refuses_without_env(kit):
    server, control, env, _ = kit
    (server / ".env").unlink()
    r = sh("bash", "ops/update.sh", cwd=server, env=env, check=False)
    assert r.returncode == 1 and "No .env" in r.stdout


def test_bot_request_is_picked_up(kit, monkeypatch):
    server, control, env, _ = kit
    import hostctl
    control.mkdir(exist_ok=True)
    monkeypatch.setattr(hostctl, "CONTROL", control)
    assert "Update requested" in hostctl.request_update("main")
    assert (control / "update-request").read_text() == "main"
    assert "doesn't look like" in hostctl.request_update("main; rm -rf /")
    r = sh("bash", "ops/update-from-request.sh", cwd=server, env=dict(env, FAKE_DOCKER_HEALTH="healthy"), check=False)
    assert r.returncode == 0 and not (control / "update-request").exists() and (server / "NEW").exists()
    assert any("Kit version" in line for line in hostctl.summary_lines())
