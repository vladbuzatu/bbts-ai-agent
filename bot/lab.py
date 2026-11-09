"""CarStage Model Lab: runs the owner's real CarStage pipelines for the `model-lab` studio agent.

Security model (same idea as the GitHub token):
- The provider keys (LAB_GEMINI_API_KEY / LAB_OPENAI_API_KEY) live only in the bot.
- The CarStage code is mounted read-only at /lab/carstage (host: /opt/carstage).
- A run happens as the separate `lab` user, in a private temp folder, with the keys in its
  environment (not on the command line, so `ps` can't show them). The agent only chooses
  WHICH pipeline, models and settings run; it can't change the code that runs with the keys.
- Every run is charged an estimated cost against the job's budget before it starts.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

import sandbox

CODE_DIR = Path(os.environ.get("LAB_CODE_DIR", "/lab/carstage"))
VENV_PY = Path(os.environ.get("LAB_PYTHON", "/opt/lab-venv/bin/python"))
LAB_USER = os.environ.get("LAB_USER", "lab")
LAB_HOME = Path(os.environ.get("LAB_HOME", "/var/lib/carstage-lab"))
RUNNER = Path(__file__).parent / "tools" / "carstage_runner.py"
RUN_TIMEOUT_S = int(os.environ.get("LAB_RUN_TIMEOUT_S", "420"))
KEY_NAMES = {"GEMINI_API_KEY": "LAB_GEMINI_API_KEY", "OPENAI_API_KEY": "LAB_OPENAI_API_KEY"}

# pipeline -> description, provider, image-model calls (setting, CarStage default), extra text calls.
# Call counts are read from the CarStage code; prices below are ESTIMATES for the budget guard.
PIPELINES: dict[str, dict] = {
    "openai_compose": {
        "desc": "Production STANDARD (1 token): rembg mask + OpenAI image edit (car + stage + prompt).",
        "provider": "OPENAI", "images": [("OPENAI_IMAGE_EDIT_MODEL", "gpt-image-1.5")], "text": 0,
        "prompt": True},
    "sec": {
        "desc": "Production ADVANCED (3 tokens): Spatial Environmental Compositor (Gemini mask, scene/"
                "plate/perspective analysis, one final Gemini composite).",
        "provider": "GEMINI", "images": [("GEMINI_RGES_MASK_MODEL", "gemini-3.1-flash-image-preview"),
                                         ("GEMINI_SEC_BACKGROUND_FIT_MODEL", "gemini-3-pro-image")], "text": 4},
    "gemini_compose": {
        "desc": "One-shot Gemini edit (process tester `gemini` mode): car + stage + prompt.",
        "provider": "GEMINI", "images": [("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image-preview")], "text": 0,
        "prompt": True},
    "photoreal": {
        "desc": "Process tester: photoreal composite (Gemini mask + shadow-catcher plate, local compositing).",
        "provider": "GEMINI", "images": [("GEMINI_PHOTOREAL_MASK_MODEL", "gemini-3.1-flash-image-preview"),
                                         ("GEMINI_PHOTOREAL_GROUND_MODEL", "gemini-3.1-flash-image-preview")],
        "text": 3},
    "computational": {
        "desc": "Process tester: computational compositor (Gemini plans, OpenCV builds).",
        "provider": "GEMINI", "images": [("GEMINI_PHOTOREAL_MASK_MODEL", "gemini-3.1-flash-image-preview"),
                                         ("GEMINI_COMP_SHADOW_MASK_MODEL", "gemini-3.1-flash-image-preview")],
        "text": 2},
    "rges": {
        "desc": "Process tester: reflective generative environment (Gemini synthesis + local reflections).",
        "provider": "GEMINI", "images": [("GEMINI_RGES_MASK_MODEL", "gemini-3.1-flash-image-preview"),
                                         ("GEMINI_RGES_SYNTH_MODEL", "gemini-3-pro-image")], "text": 3},
    "optimal": {
        "desc": "Optimal composite: local YOLO cutout + matting (Gemini mask only as a fallback when the "
                "YOLO model file is missing), wheel shadows, one final Gemini composite.",
        "provider": "GEMINI", "images": [("GEMINI_SEC_BACKGROUND_FIT_MODEL", "gemini-3-pro-image")], "text": 5},
}
TEXT_CALL_USD = 0.01
# Which pipelines the lab may run, and settings applied unless the agent sets them (owner's choice, .env).
FOCUS_PIPELINES = [p.strip() for p in os.environ.get(
    "LAB_PIPELINES", "optimal,gemini_compose,openai_compose").split(",") if p.strip()]
FOCUS_FILE = Path(os.environ.get("LAB_FOCUS_FILE", "/studio/lab/FOCUS.md"))


def default_settings(pipeline: str) -> dict[str, str]:
    """LAB_DEFAULT_SETTINGS="openai_compose:OPENAI_IMAGE_EDIT_MODEL=gpt-image-2;sec:X=Y" """
    raw = os.environ.get("LAB_DEFAULT_SETTINGS", "openai_compose:OPENAI_IMAGE_EDIT_MODEL=gpt-image-2")
    out = {}
    for item in raw.split(";"):
        if ":" in item and "=" in item:
            pipe, kv = item.split(":", 1)
            k, v = kv.split("=", 1)
            if pipe.strip() == pipeline:
                out[k.strip()] = v.strip()
    return out


def in_focus(pipeline: str) -> bool:
    return not FOCUS_PIPELINES or pipeline in FOCUS_PIPELINES
VALUE_RE = re.compile(r"^[A-Za-z0-9._:/+,x-]{1,100}$")
BLOCKED_NAME_RE = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|_DIR$|_PATH$|AUTH|ALLOWED|URL)")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def image_price(model: str, overrides: dict) -> float:
    m = model.lower()
    if "gpt-image" in m or "dall-e" in m:
        q = overrides.get("OPENAI_IMAGE_EDIT_QUALITY", "medium").lower()
        base = {"low": 0.03, "medium": 0.08, "high": 0.25}.get(q, 0.25)
        return base * (0.5 if "mini" in m else 1.0)
    if "flash" in m and "image" in m:
        return 0.07
    if "pro" in m and "image" in m:
        return 0.16
    return 0.20   # unknown model: assume expensive


def estimate(pipeline: str, overrides: dict) -> tuple[float, list[str]]:
    p = PIPELINES[pipeline]
    total, models = TEXT_CALL_USD * p["text"], []
    for setting, default in p["images"]:
        model = overrides.get(setting, default)
        models.append(model)
        total += image_price(model, overrides)
    return round(total, 3), models


def allowed_settings(code_dir: Path = CODE_DIR) -> dict[str, str]:
    """Settings the agent may override: every os.getenv("NAME", default) in CarStage's
    compose_api/config.py, minus anything secret-like or path-like."""
    cfg = code_dir / "backend" / "compose_api" / "config.py"
    if not cfg.exists():
        return {}
    out = {}
    for name, default in re.findall(r'os\.getenv\(\s*"([A-Z0-9_]+)"\s*,\s*"([^"]*)"', cfg.read_text()):
        if not BLOCKED_NAME_RE.search(name):
            out[name] = default
    return out


def _keys() -> dict[str, str]:
    out = {}
    for name, lab_name in KEY_NAMES.items():
        v = sandbox.secret(lab_name) or sandbox.secret(name)
        if v:
            out[name] = v
    return out


def status_text(code_dir: Path = CODE_DIR) -> str | None:
    """None when the lab is usable, otherwise what's missing (for the owner)."""
    if not (code_dir / "backend" / "compose_api").is_dir():
        return f"CarStage code not found at {code_dir} (on the server: /opt/carstage)."
    if not VENV_PY.exists():
        return "The lab Python environment is missing (rebuild the image with CARSTAGE_LAB=1)."
    if not _keys():
        return "No provider keys: add LAB_GEMINI_API_KEY and/or LAB_OPENAI_API_KEY to .env."
    return None


class CarStageLab:
    def __init__(self, budget_usd: float, code_dir: Path = CODE_DIR):
        self.budget = budget_usd
        self.code_dir = code_dir
        self._lock = asyncio.Lock()
        self.current: dict | None = None      # the run in progress, for /lab
        self.before_run = None                # async () -> note | None: refresh the code (GitHub main)
        self.code_version = lambda: {}        # () -> {"source", "sha", "at"}

    # ------------------------------------------------------------------ info
    def info(self, job: dict) -> str:
        problem = status_text(self.code_dir)
        keys = _keys()
        lines = [f"Lab status: {'READY' if not problem else 'NOT READY - ' + problem}",
                 f"Providers with keys: {', '.join(sorted(k.split('_')[0] for k in keys)) or 'none'}",
                 f"Budget this testing round: ${self._spent(job):.2f} used of ${self._budget(job):.2f} "
                 f"(estimates; a new round starts when the owner sends a new message). "
                 f"Total so far: ${float(job.get('lab_spent') or 0):.2f} over {int(job.get('lab_runs') or 0)} runs.",
                 f"Folder for inputs and results: {job.get('dir')}",
                 f"CarStage code (read-only): {self.code_dir} = {self.version_text()}. It follows GitHub main "
                 "automatically; unmerged work only gets here when the owner approves /lab sync.",
                 "", "Pipelines:"]
        for name, p in PIPELINES.items():
            if not in_focus(name):
                continue
            defaults = default_settings(name)
            est, models = estimate(name, defaults)
            ok = "" if p["provider"] + "_API_KEY" in keys else "  [no key]"
            lab_def = f" Lab defaults: {defaults}." if defaults else ""
            lines.append(f"- {name}: {p['desc']} Default image models: {', '.join(models)}.{lab_def} "
                         f"~${est:.2f}/run.{' Accepts `prompt`.' if p.get('prompt') else ''}{ok}")
        off = [n for n in PIPELINES if not in_focus(n)]
        if off:
            lines.append(f"(Not in focus, refused: {', '.join(off)}.)")
        if FOCUS_FILE.exists():
            lines += ["", f"OWNER'S FOCUS AND RULES ({FOCUS_FILE}) - follow them:", FOCUS_FILE.read_text()[:6000]]
        settings = allowed_settings(self.code_dir)
        if settings:
            lines += ["", "Settings you may override (name=CarStage default):"]
            lines += [f"  {k}={v}" for k, v in settings.items()]
        return "\n".join(lines)

    def _budget(self, job: dict) -> float:
        return float(job.get("lab_budget") or self.budget)

    def _spent(self, job: dict) -> float:
        """Spend in the current testing round (the budget applies per round)."""
        return float(job.get("lab_round_spent") or 0.0)

    # ------------------------------------------------------------------- run
    def validate(self, job: dict, args: dict) -> tuple[dict | None, str | None]:
        jd = Path(job["dir"]).resolve()
        pipeline = str(args.get("pipeline", "")).strip()
        if pipeline not in PIPELINES:
            return None, f"Unknown pipeline {pipeline!r}. Choose one of: {', '.join(PIPELINES)}."
        if not in_focus(pipeline):
            return None, (f"{pipeline} is not in the owner's focus (LAB_PIPELINES = "
                          f"{', '.join(FOCUS_PIPELINES)}).")
        files = {}
        for key in ("car", "stage"):
            raw = str(args.get(key, "")).strip()
            p = Path(raw)
            p = (p if p.is_absolute() else jd / p).resolve()
            roots = [jd] + [Path(r).resolve() for r in job.get("input_roots", [])]
            if not any(r in p.parents for r in roots) or not p.is_file() or p.is_symlink():
                where = "this job folder" if len(roots) == 1 else "the lab folder or the project"
                return None, f"{key}: {raw!r} is not a file inside {where}."
            if p.suffix.lower() not in IMAGE_EXT or p.stat().st_size > 25 * 1024 * 1024:
                return None, f"{key}: must be a .jpg/.png/.webp image under 25 MB."
            files[key] = p
        allowed = allowed_settings(self.code_dir)
        overrides = {k: v for k, v in default_settings(pipeline).items() if k in allowed}
        for k, v in (args.get("settings") or {}).items():
            k, v = str(k).strip(), str(v).strip()
            if k not in allowed:
                return None, f"Setting {k!r} can't be changed here (not in CarStage's config, or protected)."
            if not VALUE_RE.match(v):
                return None, f"Setting {k}: value {v!r} has characters that aren't allowed."
            overrides[k] = v
        prompt = str(args.get("prompt") or "").strip()
        if prompt and not PIPELINES[pipeline].get("prompt"):
            return None, f"Pipeline {pipeline} doesn't take a prompt (its prompts are part of the code)."
        if len(prompt) > 6000:
            return None, "Prompt too long (max 6000 characters)."
        need = PIPELINES[pipeline]["provider"] + "_API_KEY"
        if need not in _keys():
            return None, f"No {PIPELINES[pipeline]['provider'].title()} key is configured for the lab."
        label = re.sub(r"[^a-z0-9-]+", "-", str(args.get("label") or "").lower()).strip("-")[:40]
        return {"pipeline": pipeline, "files": files, "overrides": overrides, "prompt": prompt,
                "label": label}, None

    def version_text(self) -> str:
        v = self.code_version() or {}
        if not v.get("source"):
            return "the copy installed on the server (not linked to GitHub yet)"
        when = time.strftime("%d %b %H:%M", time.localtime(float(v.get("at") or 0)))
        return f"{v['source']}{' @ ' + v['sha'][:7] if v.get('sha') else ''} (since {when})"

    async def run(self, job: dict, args: dict, save_state) -> str:
        note = None
        if self.before_run:
            try:
                note = await self.before_run()
            except Exception as e:   # never block a run on the refresh
                note = f"(couldn't check GitHub for new code: {e})"
        problem = status_text(self.code_dir)
        if problem:
            return f"Refused: the lab isn't ready. {problem}"
        req, err = self.validate(job, args)
        if err:
            return f"Refused: {err}"
        est, models = estimate(req["pipeline"], req["overrides"])
        spent, budget = self._spent(job), self._budget(job)
        if spent + est > budget + 1e-9:
            return (f"Refused: budget. This run is ~${est:.2f}; ${spent:.2f} of the ${budget:.2f} for this testing "
                    "round is used. Report what you have and ask the owner whether to run another round "
                    "(their reply starts a new round with a fresh budget).")
        async with self._lock:
            job["lab_round_spent"] = round(spent + est, 3)   # charged up front: the API may bill on failure
            job["lab_spent"] = round(float(job.get("lab_spent") or 0) + est, 3)
            job["lab_runs"] = int(job.get("lab_runs") or 0) + 1
            save_state()
            n = job["lab_runs"]
            dest = Path(job["dir"]) / "lab" / f"{n:03d}-{req['pipeline']}{'-' + req['label'] if req['label'] else ''}"
            self.current = {"who": job.get("agent", "?"), "run": n, "pipeline": req["pipeline"],
                            "label": req["label"], "car": req["files"]["car"].name, "started": time.time()}
            try:
                summary = await asyncio.to_thread(self._execute, req, dest, job.get("agent", ""))
            finally:
                self.current = None
            if summary.get("not_started"):          # refund: no API call can have happened
                job["lab_round_spent"] = round(max(0.0, float(job["lab_round_spent"]) - est), 3)
                job["lab_spent"] = round(max(0.0, float(job["lab_spent"]) - est), 3)
                job["lab_runs"] -= 1
                save_state()
                return (f"Lab error (not charged): {summary['error']} This is a problem with the lab setup, "
                        "not your request. Tell the owner and stop running tests.")
        if note:
            summary["code_update"] = note
        summary["code"] = self.version_text()
        summary.update({"run": n, "folder": str(dest), "estimated_cost_usd": est, "image_models": models,
                        "budget": f"${job['lab_spent']:.2f} of ${budget:.2f} used"})
        return json.dumps(summary, indent=2)

    def _execute(self, req: dict, dest: Path, job_owner: str = "") -> dict:
        runs = LAB_HOME / "runs"
        work = runs / uuid.uuid4().hex[:12]
        work.mkdir(parents=True, exist_ok=True)
        inputs = {}
        for key, src in req["files"].items():
            p = work / f"_in_{key}{src.suffix.lower()}"
            shutil.copyfile(src, p)
            inputs[key] = str(p)
        request = {"code_dir": str(self.code_dir), "pipeline": req["pipeline"], "out_dir": str(work),
                   "prompt": req["prompt"], **inputs}
        (work / "request.json").write_text(json.dumps(request))
        cache = LAB_HOME / "cache"
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME": str(LAB_HOME), "LANG": "C.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "U2NET_HOME": os.environ.get("U2NET_HOME", "/opt/lab-models"),
               "NUMBA_CACHE_DIR": str(cache / "numba"), "MPLCONFIGDIR": str(cache / "mpl"),
               "COMPOSE_UPLOAD_DIR": str(work / "_uploads"), "COMPOSE_OUTPUT_DIR": str(work / "_outputs"),
               "SHOWROOM_ANALYSIS_CACHE_DIR": str(cache / "showroom_analysis"),
               "OMP_NUM_THREADS": "2", **req["overrides"], **_keys()}
        cmd = [str(VENV_PY), "-I", str(RUNNER), str(work / "request.json")]
        if sandbox.is_root() and _lab_user_exists():
            for d in (cache, LAB_HOME / "runs"):
                d.mkdir(parents=True, exist_ok=True)
            subprocess.run(["chown", "-R", f"{LAB_USER}:{LAB_USER}", str(work), str(cache)], check=False)
            runuser = shutil.which("runuser") or shutil.which("runuser", path="/usr/sbin:/sbin") or "/usr/sbin/runuser"
            cmd = [runuser, "-u", LAB_USER, "--", *cmd]
        t0 = time.time()
        try:
            proc = subprocess.Popen(cmd, env=env, cwd=str(work), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
        except OSError as e:   # the run never started: nothing was sent to any provider
            shutil.rmtree(work, ignore_errors=True)
            return {"ok": False, "not_started": True, "error": f"The lab could not start the run: {e}"}
        try:
            out, errout = proc.communicate(timeout=RUN_TIMEOUT_S)
            timed_out = False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, errout = proc.communicate()
            timed_out = True
        result = {"ok": False, "seconds": round(time.time() - t0, 1)}
        meta = {}
        try:
            meta = json.loads((work / "meta.json").read_text())
        except (OSError, ValueError):
            pass
        result["ok"] = bool(meta.get("ok")) and not timed_out
        if timed_out:
            result["error"] = f"Timed out after {RUN_TIMEOUT_S}s."
        elif not result["ok"]:
            result["error"] = _scrub(str(meta.get("error") or errout[-1500:] or "runner failed"))
        # Bring results into the job folder (images + meta only), then remove the work folder.
        dest.mkdir(parents=True, exist_ok=True)
        files = []
        for f in sorted(work.glob("*")):
            if f.is_file() and not f.is_symlink() and not f.name.startswith("_") and f.suffix in (".png", ".json") and f.name != "request.json" \
                    and f.stat().st_size < 60 * 1024 * 1024:
                data = f.read_bytes()
                if f.suffix == ".json":
                    data = _scrub(data.decode("utf-8", "replace")).encode()
                (dest / f.name).write_bytes(data)
                files.append(f.name)
        (dest / "settings.json").write_text(json.dumps(
            {"pipeline": req["pipeline"], "settings": req["overrides"], "prompt": req["prompt"] or None,
             "car": str(req["files"]["car"]), "stage": str(req["files"]["stage"])}, indent=2))
        if job_owner == "pm":
            sandbox.give_to_agent(dest.parent)
        else:
            sandbox.give_to_studio(dest.parent)
        shutil.rmtree(work, ignore_errors=True)
        result["files"] = files
        if meta.get("models"):
            result["models_used"] = json.loads(_scrub(json.dumps(meta["models"])))
        if meta.get("steps_ms") or meta.get("elapsed_ms"):
            result["timing_ms"] = meta.get("steps_ms") or meta.get("elapsed_ms")
        return result


def _lab_user_exists() -> bool:
    import pwd
    try:
        pwd.getpwnam(LAB_USER)
        return True
    except KeyError:
        return False


def _scrub(text: str) -> str:
    for v in _keys().values():
        if v:
            text = text.replace(v, "***")
    return text


# ---------------------------------------------------------------- agent tools
def make_tools(lab: "CarStageLab", get_job, save_state, log) -> list:
    """The two MCP tools, for the model-lab studio agent and for the PM."""
    from claude_agent_sdk import tool

    @tool("carstage_lab_info", "CarStage lab status: pipelines, default models, settings you may "
          "override, providers with keys, and the budget. Call it first.", {})
    async def carstage_lab_info(args):
        return {"content": [{"type": "text", "text": lab.info(get_job())}]}

    @tool("run_carstage_pipeline",
          "Run ONE real CarStage pipeline (the owner-approved code in the lab, read-only) on one car photo "
          "+ one stage image. Costs real money (estimated and charged to the budget before it runs). "
          "Results go to lab/<NNN>-<pipeline>-<label>/ (final.png, intermediates, meta.json, settings.json).",
          {"type": "object", "properties": {
              "pipeline": {"type": "string", "description": "openai_compose | sec | gemini_compose | "
                           "photoreal | computational | rges | optimal"},
              "car": {"type": "string", "description": "car photo path"},
              "stage": {"type": "string", "description": "stage/showroom image path"},
              "settings": {"type": "object", "additionalProperties": {"type": "string"},
                           "description": "CarStage config overrides, e.g. "
                           "{\"GEMINI_SEC_BACKGROUND_FIT_MODEL\": \"gemini-3.1-flash-image-preview\"}"},
              "prompt": {"type": "string", "description": "only for openai_compose / gemini_compose"},
              "label": {"type": "string", "description": "short name for this variant, e.g. sec-flash"}},
           "required": ["pipeline", "car", "stage"]})
    async def run_carstage_pipeline(args):
        if isinstance(args.get("settings"), str):
            try:
                args["settings"] = json.loads(args["settings"] or "{}")
            except ValueError:
                return {"content": [{"type": "text", "text": "Refused: settings must be an object."}]}
        text = await lab.run(get_job(), args, save_state)
        log(f"LAB run: {text[:300]!r}")
        return {"content": [{"type": "text", "text": text}]}

    return [carstage_lab_info, run_carstage_pipeline]


# ------------------------------------------------- owner-approved code sync
SYNC_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".ruff_cache", ".pytest_cache", ".venv", "venv",
                  ".angular", "dist", "build", ".claude", "yolov8_venv", "plates_dataset"}
SYNC_SKIP_FILES = {"backend/bg_replace/settings.py"}
SUSPICIOUS_RE = re.compile(r"requests\.|urlopen|socket\.|subprocess|os\.system|popen|os\.environ\b|"
                           r"environ\.copy|base64\.b64encode|eval\(|exec\(|__import__|/proc/|"
                           r"ftplib|smtplib|paramiko")


def _tree(root: Path) -> dict[str, Path]:
    out = {}
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SYNC_SKIP_DIRS]
        for name in filenames:
            f = Path(dirpath) / name
            rel = f.relative_to(root).as_posix()
            if f.is_symlink() or rel in SYNC_SKIP_FILES or name == ".env" or name.endswith(
                    (".pyc", ".log", ".onnx", ".pt", ".pth", ".safetensors", ".ckpt", ".bin")):
                continue
            out[rel] = f
    return out


def sync_plan(src: Path, dst: Path) -> dict:
    """What `/lab sync` would change in the lab's code, plus lines worth a human look."""
    new, old = _tree(src), _tree(dst)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(r for r in set(new) & set(old) if new[r].read_bytes() != old[r].read_bytes())
    flags = []
    for rel in added + changed:
        if not rel.endswith(".py"):
            continue
        before = set(old[rel].read_text(errors="replace").splitlines()) if rel in old else set()
        for i, line in enumerate(new[rel].read_text(errors="replace").splitlines(), 1):
            if line not in before and SUSPICIOUS_RE.search(line):
                flags.append(f"{rel}:{i}: {line.strip()[:140]}")
    return {"added": added, "changed": changed, "removed": removed, "flags": flags}


def sync_apply(src: Path, dst: Path) -> int:
    plan = sync_plan(src, dst)
    new = _tree(src)
    for rel in plan["added"] + plan["changed"]:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(new[rel], target)
        os.chmod(target, 0o644)
    for rel in plan["removed"]:
        (dst / rel).unlink(missing_ok=True)
    for dirpath, dirnames, _ in os.walk(dst):
        for d in dirnames:
            os.chmod(Path(dirpath) / d, 0o755)
    if sandbox.is_root():
        subprocess.run(["chown", "-R", "root:root", str(dst)], check=False)
    return len(plan["added"]) + len(plan["changed"]) + len(plan["removed"])


def new_round(job: dict) -> None:
    """The owner sent a new message: the next testing round gets a fresh budget."""
    if float(job.get("lab_round_spent") or 0) > 0:
        job["lab_round"] = int(job.get("lab_round") or 1) + 1
    job["lab_round_spent"] = 0.0


def recent_runs(job_dirs: list[Path], n: int = 8) -> list[dict]:
    """Newest lab runs across the given job folders (from their result folders)."""
    runs = []
    for jd in job_dirs:
        for d in (Path(jd) / "lab").glob("[0-9][0-9][0-9]-*"):
            if not d.is_dir():
                continue
            meta, settings = {}, {}
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, ValueError):
                pass
            try:
                settings = json.loads((d / "settings.json").read_text())
            except (OSError, ValueError):
                pass
            runs.append({"name": d.name, "when": d.stat().st_mtime, "ok": bool(meta.get("ok")),
                         "error": (meta.get("error") or "")[:120], "seconds": round((meta.get("wall_ms") or 0) / 1000),
                         "car": Path(settings.get("car", "")).name, "where": Path(jd).name})
    return sorted(runs, key=lambda r: r["when"], reverse=True)[:n]
