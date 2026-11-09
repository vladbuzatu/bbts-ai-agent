#!/usr/bin/env python3
"""Run ONE CarStage pipeline on one car photo + one stage (showroom) image.

Executed by the bot as the unprivileged `lab` user with the lab venv. The CarStage
code is mounted read-only; this script only imports and calls it. Model and tuning
overrides arrive as environment variables (validated by the bot), exactly the way
CarStage itself is configured in production.

    carstage_runner.py <request.json>
    request: {"code_dir", "pipeline", "car", "stage", "out_dir", "prompt"?}

Writes into out_dir: final.png, any intermediate images the pipeline returns
(<name>.png) and meta.json (timings, models, settings, or the error).
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

# pipeline -> (module, function); all take (car_bytes, car_mime, stage_bytes, stage_mime)
PIPELINES = {
    "sec": ("compose_api.spatial_environmental_compositor", "run_spatial_environmental_compositor"),
    "photoreal": ("compose_api.photoreal_composite_pipeline", "run_photoreal_composite"),
    "computational": ("compose_api.computational_automotive_compositor", "run_computational_composite"),
    "rges": ("compose_api.reflective_generative_environment", "run_reflective_generative_environment"),
    "optimal": ("compose_api.optimal_composite_pipeline", "run_optimal_composite"),
}


def _mime(b: bytes) -> str:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _is_image(b: bytes) -> bool:
    return b[:8] == b"\x89PNG\r\n\x1a\n" or b[:3] == b"\xff\xd8\xff" or (b[:4] == b"RIFF" and b[8:12] == b"WEBP")


def _save_png(b: bytes, path: Path) -> None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        path.write_bytes(b)
        return
    import io
    from PIL import Image
    with Image.open(io.BytesIO(b)) as im:
        im.save(path, "PNG")


def _setup_django(backend: Path, out: Path) -> None:
    """Minimal Django settings: some CarStage helpers read django.conf.settings
    (BASE_DIR for model files, storage). No database, no apps, no CarStage settings.py."""
    import django
    from django.conf import settings
    settings.configure(
        BASE_DIR=backend, DEBUG=False, INSTALLED_APPS=[], USE_TZ=True,
        SECRET_KEY="carstage-lab", MEDIA_ROOT=str(out / "_media"), MEDIA_URL="/media/",
        STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                  "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}},
    )
    django.setup()


def _prepare_inputs(req: dict, out: Path) -> dict:
    from PIL import Image
    from compose_api import config
    paths = {}
    for key in ("car", "stage"):
        p = out / f"_in_{key}.png"
        p.write_bytes(Path(req[key]).read_bytes())       # production writes the raw upload like this
        with Image.open(p) as im:                        # ... and only resizes when too large
            if max(im.size) > config.MAX_IMAGE_DIMENSION:
                im.thumbnail((config.MAX_IMAGE_DIMENSION, config.MAX_IMAGE_DIMENSION))
                im.save(p)
        paths[key] = str(p)
    return paths


def _openai_compose(req: dict, out: Path) -> dict:
    """Production STANDARD path (car_photo_process.py): rembg edit mask + OpenAI image edit."""
    from compose_api import config
    from compose_api.helpers import _create_edit_mask, run_image_edit_openai
    paths = _prepare_inputs(req, out)
    mask = _create_edit_mask(paths["car"]) if config.OPENAI_USE_REMBG_MASK else None
    final = run_image_edit_openai(paths["car"], paths["stage"], prompt=req.get("prompt") or None, mask_path=mask)
    res = {"final_png": final,
           "models": {"edit": config.OPENAI_IMAGE_EDIT_MODEL},
           "openai": {"quality": config.OPENAI_IMAGE_EDIT_QUALITY, "size": config.OPENAI_IMAGE_EDIT_SIZE,
                      "rembg_mask": bool(mask), "custom_prompt": bool(req.get("prompt"))}}
    if mask:
        res["edit_mask_png"] = Path(mask).read_bytes()
    return res


def _gemini_compose(req: dict, out: Path) -> dict:
    """The one-shot Gemini edit (views_process `gemini` mode): car + stage + prompt -> image."""
    from compose_api import config
    from compose_api.helpers import run_image_edit_gemini
    paths = _prepare_inputs(req, out)
    final = run_image_edit_gemini(paths["car"], paths["stage"], prompt=req.get("prompt") or None)
    return {"final_png": final, "models": {"edit": config.GEMINI_IMAGE_MODEL},
            "gemini": {"custom_prompt": bool(req.get("prompt"))}}


def _jsonable(v, depth=0):
    if depth > 6:
        return str(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, dict):
        return {str(k): _jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x, depth + 1) for x in v][:200]
    return str(v)


def main() -> int:
    req = json.loads(Path(sys.argv[1]).read_text())
    out = Path(req["out_dir"])
    backend = Path(req["code_dir"]) / "backend"
    meta: dict = {"pipeline": req["pipeline"], "ok": False}
    t0 = time.perf_counter()
    try:
        os.chdir(out)
        sys.path.insert(0, str(backend))
        _setup_django(backend, out)
        if req["pipeline"] == "openai_compose":
            res = _openai_compose(req, out)
        elif req["pipeline"] == "gemini_compose":
            res = _gemini_compose(req, out)
        else:
            import importlib
            mod, fn = PIPELINES[req["pipeline"]]
            run = getattr(importlib.import_module(mod), fn)
            car_b, stage_b = Path(req["car"]).read_bytes(), Path(req["stage"]).read_bytes()
            res = run(car_b, _mime(car_b), stage_b, _mime(stage_b))
        images = {k: bytes(v) for k, v in res.items() if isinstance(v, (bytes, bytearray)) and _is_image(v)}
        if "final_png" not in images:
            got = res.get("final_png")
            raise RuntimeError("pipeline returned no final image"
                               + (f" (got {len(got)} bytes starting {bytes(got[:8])!r})" if got else ""))
        for k, v in images.items():
            # Providers return PNG, JPEG or WebP; store everything as PNG so results compare cleanly.
            _save_png(v, out / ("final.png" if k == "final_png" else f"{k.removesuffix('_png')}.png"))
        meta["final_format"] = _mime(images["final_png"])
        meta.update({k: _jsonable(v) for k, v in res.items() if k not in images})
        meta["ok"] = True
    except Exception as e:  # report, don't crash: the bot turns this into a tool result
        detail = ""
        resp = getattr(e, "response", None)            # httpx / openai errors: the provider's own message
        if resp is not None:
            try:
                detail = " | provider says: " + str(getattr(resp, "text", "") or "")[:800]
            except Exception:
                pass
        meta["error"] = (f"{type(e).__name__}: {e}" + detail)[:2500]
        meta["traceback"] = traceback.format_exc()[-4000:]
    meta["wall_ms"] = int((time.perf_counter() - t0) * 1000)
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps({"ok": meta["ok"], "error": meta.get("error"), "wall_ms": meta["wall_ms"]}))
    return 0 if meta["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
