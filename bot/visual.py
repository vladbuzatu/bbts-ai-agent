"""Visual regression: compare each project's screenshots (design/screens/*.png, made by the agents)
with the last approved set, and show what changed.

Baselines live in the bot's private data folder (data/visual/<project>/), so agents can't "approve"
their own changes. A baseline is accepted when the owner merges a pull request, or with
/screens accept [project]. The first screenshots of a project become its baseline.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageChops, ImageDraw, ImageOps

if TYPE_CHECKING:
    from orchestrator import Orchestrator

THRESHOLD = 0.5          # % of pixels that must differ for a screen to count as changed
TOLERANCE = 24           # per-channel difference ignored (anti-aliasing, font hinting)
MAX_SIDE = 1600          # diff images are scaled to this width (Telegram-friendly)


@dataclass
class Change:
    name: str
    kind: str            # changed | new | removed
    percent: float
    image: Path | None   # side-by-side before | after | highlighted


class Visual:
    def __init__(self, orch: "Orchestrator"):
        self.o = orch

    def _dirs(self, project: str) -> tuple[Path, Path, Path]:
        base = self.o.cfg.data_dir / "visual" / project
        return self.o.project_root(project)[1] / "design" / "screens", base / "baseline", base / "diffs"

    def _screens(self, folder: Path) -> dict[str, Path]:
        if not folder.is_dir():
            return {}
        root = folder.resolve()
        out = {}
        for p in folder.rglob("*.png"):
            r = p.resolve()
            if (root in r.parents) and r.is_file() and r.stat().st_size < 30 * 1024 * 1024:
                out[p.relative_to(folder).as_posix()] = r
        return out

    def compare(self, project: str) -> list[Change]:
        screens, baseline, diffs = self._dirs(project)
        now, before = self._screens(screens), self._screens(baseline)
        if now and not before:
            self.accept_baseline(project)          # the first screenshots become the baseline
            return []
        shutil.rmtree(diffs, ignore_errors=True)
        diffs.mkdir(parents=True, exist_ok=True)
        changes: list[Change] = []
        for name in sorted(set(now) | set(before)):
            if name not in before:
                changes.append(Change(name, "new", 100.0, None))
            elif name not in now:
                changes.append(Change(name, "removed", 100.0, None))
            else:
                pct, img = self._diff(before[name], now[name], diffs / name.replace("/", "__"))
                if pct >= THRESHOLD:
                    changes.append(Change(name, "changed", pct, img))
        return changes

    def _diff(self, a_path: Path, b_path: Path, out: Path) -> tuple[float, Path | None]:
        try:
            a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
        except OSError:
            return 0.0, None
        if a.size != b.size:
            b_cmp = b.resize(a.size)
        else:
            b_cmp = b
        delta = ImageChops.difference(a, b_cmp).convert("L").point(lambda v: 255 if v > TOLERANCE else 0)
        hist = delta.histogram()
        changed = hist[255]
        pct = 100.0 * changed / (a.size[0] * a.size[1]) + (100.0 if a.size != b.size and changed == 0 else 0)
        if pct < THRESHOLD and a.size == b.size:
            return pct, None
        overlay = ImageOps.grayscale(b_cmp).convert("RGB")
        red = Image.new("RGB", a.size, (220, 30, 30))
        overlay = Image.composite(red, overlay, delta)
        w, h = a.size
        sheet = Image.new("RGB", (w * 3 + 40, h + 40), "white")
        for i, im in enumerate((a, b_cmp, overlay)):
            sheet.paste(im, (i * (w + 20), 40))
        d = ImageDraw.Draw(sheet)
        for i, label in enumerate(("before (approved)", "now", "what changed")):
            d.text((i * (w + 20) + 8, 12), label, fill="black")
        if sheet.width > MAX_SIDE * 2:
            sheet = sheet.resize((MAX_SIDE * 2, int(sheet.height * MAX_SIDE * 2 / sheet.width)))
        out = out.with_suffix(".png")
        sheet.save(out)
        return pct, out

    def accept_baseline(self, project: str) -> int:
        screens, baseline, _ = self._dirs(project)
        now = self._screens(screens)
        shutil.rmtree(baseline, ignore_errors=True)
        for name, p in now.items():
            dest = baseline / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
        return len(now)

    def summary(self, project: str) -> str:
        changes = self.compare(project)
        if not changes:
            return ""
        parts = [f"{c.name} ({'new' if c.kind == 'new' else 'removed' if c.kind == 'removed' else f'{c.percent:.0f}%'})"
                 for c in changes[:5]]
        return f"🖼 {len(changes)} screen(s) changed vs approved: " + ", ".join(parts) + \
               (" …" if len(changes) > 5 else "") + f" (/screens diff {project})"

    async def send_diffs(self, project: str, limit: int = 4) -> int:
        if not self.o.send_image:
            return 0
        sent = 0
        for c in self.compare(project):
            if c.image and sent < limit:
                await self.o.send_image(c.image, f"{project}: {c.name} · {c.percent:.1f}% of pixels changed")
                sent += 1
        return sent

    async def send_for_pr(self, project: str, key: str) -> None:
        await self.send_diffs(project)
