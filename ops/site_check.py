#!/usr/bin/env python3
"""Safety check for a project's preview/compose.yml before ops/site.sh runs it on the host.

The compose file is written by the agents, so it is untrusted: it must not be able to reach the
host (bind mounts, privileged mode, host network, published ports, host env files, plugins…).
We check the configuration *as Docker Compose resolved it* (`docker compose config
--no-env-resolution --format json`, after includes, extends and interpolation), with an allowlist:
anything not known to be harmless is refused.

  site_check.py check <src-dir> < config.json            -> {"ok", "errors", "services", "built", "port"}
  site_check.py override <project> <id> <alias> [mem] < config.json   -> override file (JSON is YAML)

Standard library only (runs on the host's python3).
"""
from __future__ import annotations

import json
import os
import re
import sys

SERVICE_KEYS = {
    "build", "image", "command", "entrypoint", "environment", "env_file", "depends_on", "healthcheck",
    "restart", "working_dir", "user", "expose", "volumes", "labels", "networks", "mem_limit",
    "mem_reservation", "cpus", "pids_limit", "init", "tmpfs", "read_only", "shm_size", "stop_grace_period",
    "stop_signal", "stdin_open", "tty", "ulimits", "hostname", "domainname", "platform", "pull_policy",
    "extra_hosts", "dns", "dns_search", "cap_drop",
}
BUILD_KEYS = {"context", "dockerfile", "dockerfile_inline", "args", "target", "labels", "shm_size",
              "no_cache", "pull", "cache_from", "tags"}
TOP_KEYS = {"name", "services", "volumes", "networks"}
VOLUME_KEYS = {"name", "labels"}
NETWORK_KEYS = {"name", "ipam", "internal", "labels"}
WEB = "web"
PORT_LABEL = "bbts.site.port"
SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([bkmg]?)b?$", re.IGNORECASE)


def _inside(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base + os.sep)


def _size(v) -> int | None:
    if isinstance(v, (int, float)):
        return int(v)
    m = SIZE_RE.match(str(v).strip())
    if not m:
        return None
    return int(float(m.group(1)) * {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[m.group(2).lower()])


def check(cfg: dict, src: str) -> dict:
    errors: list[str] = []
    services = cfg.get("services") or {}
    for key in cfg:
        if key not in TOP_KEYS and not key.startswith("x-"):
            errors.append(f"top-level '{key}' isn't allowed on the preview server")
    for name, vol in (cfg.get("volumes") or {}).items():
        for key in vol or {}:
            if key not in VOLUME_KEYS:
                errors.append(f"volume '{name}': '{key}' isn't allowed (only plain named volumes)")
    networks = cfg.get("networks") or {}
    for name, net in networks.items():
        for key, val in (net or {}).items():
            if key not in NETWORK_KEYS or (key == "ipam" and val):
                errors.append(f"network '{name}': '{key}' isn't allowed (only plain internal networks)")
    if not services:
        errors.append("no services")
    built = []
    for name, svc in services.items():
        svc = svc or {}
        where = f"service '{name}'"
        for key in svc:
            if key not in SERVICE_KEYS:
                errors.append(f"{where}: '{key}' isn't allowed on the preview server")
        for vol in svc.get("volumes") or []:
            kind = vol.get("type")
            if kind == "volume":
                src_name = vol.get("source")
                if src_name and src_name not in (cfg.get("volumes") or {}):
                    errors.append(f"{where}: volume '{src_name}' must be declared under top-level volumes")
            elif kind != "tmpfs":
                errors.append(f"{where}: {kind} mount of {vol.get('source', '?')} isn't allowed "
                              "(use named volumes; code goes into the image with COPY)")
        for ef in svc.get("env_file") or []:
            path = ef.get("path") if isinstance(ef, dict) else ef
            if not path or not _inside(path, src):
                errors.append(f"{where}: env_file {path} is outside the project")
        for net in (svc.get("networks") or {}):
            if net not in networks:
                errors.append(f"{where}: unknown network '{net}'")
        b = svc.get("build")
        if b:
            built.append(name)
            for key in b:
                if key not in BUILD_KEYS:
                    errors.append(f"{where}: build '{key}' isn't allowed")
            ctx = str(b.get("context") or "")
            if not ctx.startswith("/") or not _inside(ctx, src):
                errors.append(f"{where}: build context {ctx} must be a folder inside the project")
            elif b.get("dockerfile") and not _inside(os.path.join(ctx, b["dockerfile"]), src):
                errors.append(f"{where}: dockerfile {b['dockerfile']} is outside the project")
        elif not svc.get("image"):
            errors.append(f"{where}: needs build or image")
    port = 80
    if WEB not in services:
        errors.append(f"there must be a service called '{WEB}' (the one the preview address shows)")
    else:
        raw = str(((services[WEB] or {}).get("labels") or {}).get(PORT_LABEL, "80"))
        if raw.isdigit() and 0 < int(raw) < 65536:
            port = int(raw)
        else:
            errors.append(f"label {PORT_LABEL} must be a port number")
    return {"ok": not errors, "errors": errors, "services": sorted(services), "built": built, "port": port}


def override(cfg: dict, project: str, ident: str, alias: str, mem: str = "512m") -> dict:
    """Our additions: resource limits, unique image tags (for rollback), the proxy network for 'web'."""
    cap = _size(mem) or 512 * 1024 ** 2
    out: dict = {"services": {}, "networks": {"bbts-proxy": {"name": "proxy", "external": True}}}
    for name, svc in (cfg.get("services") or {}).items():
        svc = svc or {}
        want = _size(svc.get("mem_limit")) if svc.get("mem_limit") else None
        o = {"mem_limit": min(want, cap) if want else cap,
             "pids_limit": 512, "cpus": 1.0, "restart": "unless-stopped",
             "labels": {"bbts.site": project}}
        if svc.get("build"):
            o["image"] = f"bbts-site-{project}-{name}:{ident}"
        if name == WEB:
            nets = {n: {} for n in (svc.get("networks") or {"default": None})}
            nets["bbts-proxy"] = {"aliases": [alias]}
            o["networks"] = nets
        out["services"][name] = o
    return out


def main(argv: list[str]) -> int:
    cfg = json.load(sys.stdin)
    if len(argv) >= 3 and argv[1] == "check":
        print(json.dumps(check(cfg, argv[2])))
        return 0
    if len(argv) >= 5 and argv[1] == "override":
        print(json.dumps(override(cfg, argv[2], argv[3], argv[4], *(argv[5:6] or ["512m"])), indent=1))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
