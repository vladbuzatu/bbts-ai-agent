"""The bot's side of the host control channel (/control, bind-mounted from /var/lib/bbts/control).

The bot can't run docker or touch the kit's files on the host (by design). It asks the host's systemd
units (installed by ops/install.sh) to do things by writing request files, and reads their status files.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

CONTROL = Path(os.environ.get("CONTROL_DIR", "/control"))
DATA = Path(os.environ.get("DATA_DIR", "/data"))
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
BOOT = int(time.time())          # when this bot process started (ops/update.sh checks it)


def heartbeat() -> None:
    """Touched every 20 s by the background loop; the container healthcheck reads its age."""
    try:
        (DATA / "heartbeat").write_text(f"{BOOT} {int(time.time())}")
    except OSError:
        pass


def _read(name: str) -> dict:
    try:
        return json.loads((CONTROL / name).read_text())
    except (OSError, ValueError):
        return {}


def available() -> bool:
    """True when the host side (ops/install.sh) is installed."""
    return CONTROL.is_dir() and any(CONTROL.iterdir())


def request_update(ref: str = "main") -> str:
    if not CONTROL.is_dir():
        return "The update service isn't installed on the server yet: run  sudo ops/install.sh  in the kit folder."
    if not REF_RE.match(ref):
        return "That doesn't look like a branch, tag or commit."
    (CONTROL / "update-request").write_text(ref)
    return ("🔄 Update requested. The bot restarts in a minute or two and reports back here "
            "(it rolls back by itself if the new version doesn't start).") if ref != "check" else \
        "🔎 Checking GitHub for a newer version…"


def _ago(ts) -> str:
    if not ts:
        return "never"
    s = int(time.time() - float(ts))
    return f"{s // 60} min ago" if s < 3600 else f"{s // 3600} h ago" if s < 86400 else f"{s // 86400} d ago"


def summary_lines() -> list[str]:
    """For /server and /update."""
    if not CONTROL.is_dir():
        return ["🔧 Host services: not installed (sudo ops/install.sh)"]
    out = []
    up, av = _read("update-status.json"), _read("update-available.json")
    ver = up.get("version") or av.get("version") or "?"
    line = f"🏷 Kit version {ver}"
    if av.get("behind"):
        line += f" · ⬆️ {av['behind']} newer commit(s): {av.get('latest', '')[:60]} (/update)"
    elif av:
        line += " · up to date"
    out.append(line)
    if up.get("state") in ("failed", "rolled_back"):
        out.append(f"   last update: {up['state'].replace('_', ' ')} – {up.get('message', '')[:80]} ({_ago(up.get('at'))})")
    bk = _read("backup-status.json")
    if bk:
        out.append(f"💾 Backup {bk.get('day')}: {'✅' if bk.get('ok') else '⚠️ ' + ' '.join(bk.get('failed', []))} "
                   f"{bk.get('size', '')} {bk.get('offsite', '')}".rstrip())
    else:
        out.append("💾 Backup: none yet (runs nightly at 03:40)")
    return out
