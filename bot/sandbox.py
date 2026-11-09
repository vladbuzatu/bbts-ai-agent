"""Privilege separation between the bot and the agents.

The bot runs as root *inside the container* and holds the secrets (Telegram and
GitHub tokens). Agents, tests, previews and every git command on the agents'
project run as the unprivileged `agent` user with no secrets in their environment.

Why git also runs as `agent`: a repository's own config/hooks/.gitattributes can
execute commands. Running git as root on a folder the agents control would let a
planted hook run with the bot's privileges. Pushing happens from a separate
root-owned mirror (see Orchestrator.push), never from the agents' folder.

Outside a root container (e.g. running the tests on a laptop) everything degrades
gracefully to "same user", so the code paths stay identical.
"""
from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

AGENT_USER = os.environ.get("AGENT_USER", "agent")
SECRET_KEYS = ("TELEGRAM_BOT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
               "LAB_GEMINI_API_KEY", "LAB_OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY")
_secrets: dict[str, str] = {}
# Largest single message from the Claude CLI (an agent reading a big image produces one).
SDK_MAX_BUFFER = 64 * 1024 * 1024


def load_and_scrub() -> dict[str, str]:
    """Read secrets once, then remove them from this process's environment so no
    child process (agents, tests, previews) can inherit them."""
    for k in list(SECRET_KEYS) + [k for k in os.environ if k.startswith("GITHUB_TOKEN_")]:
        v = os.environ.pop(k, None)
        if v:
            _secrets[k] = v
    return dict(_secrets)


def secret(key: str) -> str | None:
    return _secrets.get(key) or os.environ.get(key)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def agent_pw():
    try:
        return pwd.getpwnam(AGENT_USER)
    except KeyError:
        return None


def separated() -> bool:
    """True when the bot can and will run agents as a different user."""
    return is_root() and agent_pw() is not None


def agent_user() -> str | None:
    return AGENT_USER if separated() else None


def agent_home() -> Path:
    pw = agent_pw()
    return Path(pw.pw_dir) if pw else Path.home()


def agent_env(extra: dict | None = None) -> dict[str, str]:
    """Overrides for the agent's environment (the SDK merges these over os.environ,
    which has already been scrubbed). Blank secrets are a second safety net."""
    env = {k: "" for k in SECRET_KEYS}
    env.update({"CI": "1", "NG_CLI_ANALYTICS": "false"})   # no watch mode, no prompts
    if separated():
        home = str(agent_home())
        env.update({"HOME": home, "USER": AGENT_USER, "LOGNAME": AGENT_USER, "PIP_USER": "1",
                    "NPM_CONFIG_PREFIX": f"{home}/.npm-global",
                    "PATH": f"{home}/.npm-global/bin:{home}/.local/bin:" + os.environ.get("PATH", "")})
    env.update(extra or {})
    return env


def as_agent(cmd: list[str]) -> list[str]:
    """Prefix a command so it runs as the agent user (no-op when not separated)."""
    if not separated():
        return cmd
    home = str(agent_home())
    return ["runuser", "-u", AGENT_USER, "--", "env", f"HOME={home}", f"USER={AGENT_USER}", "PIP_USER=1",
            f"NPM_CONFIG_PREFIX={home}/.npm-global",
            f"PATH={home}/.npm-global/bin:{home}/.local/bin:" + os.environ.get("PATH", ""), *cmd]


def git(*args: str, cwd: Path | str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """git on the agents' project, always as the agent user."""
    return subprocess.run(as_agent(["git", *args]), cwd=cwd, capture_output=capture, text=True, check=check)


def give_to_agent(*paths: Path | str) -> None:
    """Hand files the bot created (uploads, synced templates) to the agent user."""
    if not separated():
        return
    for p in paths:
        p = Path(p)
        if p.exists():
            subprocess.run(["find", str(p), "-path", "*/node_modules", "-prune", "-o",
                            "!", "-user", AGENT_USER, "-exec", "chown", "-h", f"{AGENT_USER}:{AGENT_USER}", "{}", "+"],
                           check=False, capture_output=True)


def lock_down(path: Path) -> None:
    """Make a bot-only directory (state, mirror, secrets): root-owned, mode 700."""
    path.mkdir(parents=True, exist_ok=True)
    if is_root():
        subprocess.run(["chown", "-R", "root:root", str(path)], check=False)
        os.chmod(path, 0o700)


def kill_agent_processes() -> int:
    """Kill everything running as the agent user (leftover test runners, dev servers,
    headless browsers). Used by /stop so stopping really stops. Returns the count."""
    if not separated():
        return 0
    total = 0
    for user in (AGENT_USER, os.environ.get("LAB_USER", "lab")):
        try:
            pwd.getpwnam(user)
            out = subprocess.run(["pgrep", "-u", user], capture_output=True, text=True).stdout.split()
            if out:
                subprocess.run(["pkill", "-KILL", "-u", user], capture_output=True)
            total += len(out)
        except (KeyError, FileNotFoundError):
            continue
    return total
