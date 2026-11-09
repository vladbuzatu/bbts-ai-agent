"""Visual regression: baselines, diffs, approval on merge."""
from __future__ import annotations

from PIL import Image, ImageDraw


def shot(path, color=(240, 240, 240), box=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (400, 300), color)
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=(185, 28, 28))
    im.save(path)


def test_first_screens_become_baseline_then_changes_are_found(make_orch):
    o = make_orch(multi=True)
    screens = o.cfg.workspace / "design" / "screens"
    shot(screens / "home-1440.png")
    shot(screens / "login-390.png")
    assert o.visual.compare("app") == []                       # baseline saved, nothing to report
    shot(screens / "home-1440.png", box=(50, 50, 250, 200))    # a big red block appears
    shot(screens / "plans-1440.png")                           # a new screen
    changes = {c.name: c for c in o.visual.compare("app")}
    assert changes["home-1440.png"].kind == "changed" and changes["home-1440.png"].percent > 20
    assert changes["home-1440.png"].image.exists()
    assert changes["plans-1440.png"].kind == "new" and "login-390.png" not in changes
    assert "2 screen(s) changed" in o.visual.summary("app")
    o.visual.accept_baseline("app")
    assert o.visual.compare("app") == []


def test_tiny_rendering_noise_is_ignored(make_orch):
    o = make_orch(multi=True)
    screens = o.cfg.workspace / "design" / "screens"
    shot(screens / "a.png")
    o.visual.compare("app")
    shot(screens / "a.png", color=(236, 236, 236))             # anti-aliasing-level change
    assert o.visual.compare("app") == []
