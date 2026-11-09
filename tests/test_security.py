"""Privilege separation and the deploy gate (incl. real rollback logic)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import sandbox
from conftest import FAKES, KIT, run

needs_separation = pytest.mark.skipif(not sandbox.separated(),
                                      reason="needs root with an 'agent' user (run inside the container)")


def test_secrets_are_removed_from_the_environment():
    for k in sandbox.SECRET_KEYS:
        assert k not in os.environ
    assert sandbox.secret("GITHUB_TOKEN")


@needs_separation
def test_agent_processes_cannot_see_or_read_secrets(make_orch):
    o = make_orch(github=True)
    probe = (f"id -un; env | grep -c -E 'TEST_SECRET' ; "
             f"cat {o.cfg.data_dir}/state.json >/dev/null 2>&1 && echo READ || echo DENIED; "
             f"cat /root/.git-credentials >/dev/null 2>&1 && echo READ || echo DENIED")
    out = subprocess.run(sandbox.as_agent(["sh", "-c", probe]), capture_output=True, text=True).stdout.split()
    assert out == ["agent", "0", "DENIED", "DENIED"]
    assert o._options().user == "agent"


@needs_separation
def test_tests_and_git_run_as_agent_and_planted_hooks_never_run_as_root(make_orch, base):
    o = make_orch(github=True)
    (o.cfg.workspace / "scripts/verify.sh").write_text('echo "::: whoami $(id -un) OK"; echo "VERIFY: PASS"')
    run(o.verify())
    assert "whoami agent" in o.state.last_verify
    marker = base / "hook-ran-as"
    for hook in ("pre-push", "post-commit", "reference-transaction"):
        h = o.cfg.workspace / ".git/hooks" / hook
        h.write_text(f"#!/bin/sh\nid -un >> {marker}\n")
        h.chmod(0o755)
    sandbox.git("config", "core.fsmonitor", f"id -un >> {marker}; false", cwd=o.cfg.workspace)
    os.chmod(base, 0o777)
    (o.cfg.workspace / "f.txt").write_text("x")
    sandbox.git("add", "f.txt", cwd=o.cfg.workspace)
    sandbox.git("commit", "-qm", "T-01: f", cwd=o.cfg.workspace)
    assert "Pushed" in run(o.push())
    ran_as = set(marker.read_text().split()) if marker.exists() else set()
    assert "root" not in ran_as


def test_ask_is_read_only(make_orch, monkeypatch):
    import extras
    o = make_orch()
    seen = {}

    def fake(prompt, options):
        seen["opts"] = options

        async def g():
            from conftest import result
            yield result("The import validates each row in serializers.py.")
        return g()
    monkeypatch.setattr(extras, "query", fake)
    answer = run(extras.ask(o, "backend-dev", "how is the import validated?"))
    assert "backend-dev" in answer and "serializers.py" in answer
    assert seen["opts"].tools == ["Read", "Grep", "Glob"]
    assert "Bash" in seen["opts"].disallowed_tools and "Write" in seen["opts"].disallowed_tools


# ------------------------------------------------------------------ deploy gate
GATE = KIT / "workspace-template/deploy/deploy-gate"


@pytest.fixture
def gate(base):
    app = base / "app"
    for d in ("staging", "production", "backups"):
        (app / d).mkdir(parents=True)
    for e in ("staging", "production"):
        (app / e / "compose.yml").touch()
    env = dict(os.environ, DEPLOY_GATE_ROOT=str(app), FAKE_STATE=str(base / "state"),
               PATH=f"{FAKES}:{os.environ['PATH']}", GATE_TRIES="2", GATE_SLEEP="0")

    def call(cmd: str):
        p = subprocess.run(["bash", str(GATE), cmd], input="registry-token\n", capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr

    def running(e):
        f = base / "state" / f"app-{e}.running"
        return f.read_text().strip() if f.exists() else None

    def deployed(e):
        f = app / e / "DEPLOYED_TAG"
        return f.read_text().strip() if f.exists() else None
    call.running, call.deployed, call.app = running, deployed, app
    return call


def test_gate_deploys_promotes_and_rolls_back(gate):
    assert gate("deploy staging aaaaaaa owner/app me")[0] == 0
    assert gate("promote production owner/app me")[0] == 0
    assert gate.running("production") == "aaaaaaa"
    # broken release: must be rolled back automatically
    rc, out = gate("deploy staging badc0de owner/app me")
    assert rc == 2 and "ROLLED BACK" in out
    assert gate.running("staging") == gate.deployed("staging") == "aaaaaaa"
    # failed migrations and failed downloads never replace the running version
    assert gate("deploy staging baadf00d owner/app me")[0] == 1
    assert gate("deploy staging 0badfee owner/app me")[0] == 1
    assert gate.running("staging") == "aaaaaaa"
    # production gets a DB backup before each deploy once it has a database
    assert gate("deploy staging bbbbbbb owner/app me")[0] == 0
    assert gate("promote production owner/app me")[0] == 0
    assert gate.running("production") == "bbbbbbb"
    assert list((gate.app / "backups").glob("pre-deploy-production-*.sql.gz"))
    history = (gate.app / "staging" / "history.log").read_text()
    for result_name in ("ok", "rolled_back", "migrate_failed", "pull_failed"):
        assert f"result={result_name}" in history


def test_gate_fire_drill_passes(gate):
    gate("deploy staging aaaaaaa owner/app me")
    rc, out = gate("drill staging owner/app me")
    assert rc == 0 and "DRILL PASSED" in out
    assert gate.running("staging") == "aaaaaaa"


@pytest.mark.parametrize("cmd,reason", [
    ("deploy production aaaaaaa owner/app me", "only for staging"),
    ("deploy staging not-a-sha owner/app me", "bad tag"),
    ("deploy staging aaaaaaa owner/app me;rm", "bad actor"),
    ("rm -rf /", "unknown command"),
    ("promote staging owner/app me", "only targets production"),
    ("drill production owner/app me", "only run on staging"),
    ("", "unknown command"),
])
def test_gate_refuses_anything_else(gate, cmd, reason):
    rc, out = gate(cmd)
    assert rc == 64 and reason in out


def test_gate_pins_the_first_repository(gate):
    gate("deploy staging aaaaaaa owner/app me")
    rc, out = gate("deploy staging bbbbbbb attacker/app me")
    assert rc == 64 and "only accepts deploys from owner/app" in out


def test_gate_refuses_promotion_when_staging_has_nothing(gate):
    rc, out = gate("promote production owner/app me")
    assert rc == 64 and "nothing healthy is deployed on staging" in out


def test_forced_command_passes_one_argument_without_shell_injection(base):
    marker = base / "INJECTED"
    hostile = f"deploy staging aaaaaaa owner/app me; touch {marker} $(touch {marker}2)"
    out = subprocess.run(["sh", "-c", 'printf "%s" "$SSH_ORIGINAL_COMMAND"'], capture_output=True, text=True,
                         env=dict(os.environ, SSH_ORIGINAL_COMMAND=hostile)).stdout
    assert out == hostile and not marker.exists() and not Path(f"{marker}2").exists()
