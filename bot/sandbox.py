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
               "LAB_GEMINI_API_KEY", "LAB_OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY",
               "VOICE_OPENAI_API_KEY", "DASHBOARD_TOKEN")
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


STUDIO_USER = os.environ.get("STUDIO_USER", "studio")
PROXY_PORT = int(os.environ.get("EGRESS_PROXY_PORT", "3128"))


def egress_restricted() -> bool:
    """AGENT_EGRESS=restricted: the build team reaches the internet only through the bot's allowlist proxy."""
    return os.environ.get("AGENT_EGRESS", "open").lower() == "restricted" and separated()


def proxy_env() -> dict[str, str]:
    if not egress_restricted():
        return {}
    url = f"http://127.0.0.1:{PROXY_PORT}"
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url,
            "NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}


def _user_exists(user: str) -> bool:
    try:
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False


def studio_user() -> str | None:
    """Studio jobs run as their own user (open internet for research), when the image has it."""
    if separated() and _user_exists(STUDIO_USER):
        return STUDIO_USER
    return agent_user()


def _home(user: str) -> str:
    return pwd.getpwnam(user).pw_dir


def _user_env(user: str) -> dict[str, str]:
    home = _home(user)
    return {"HOME": home, "USER": user, "LOGNAME": user, "PIP_USER": "1",
            "NPM_CONFIG_PREFIX": f"{home}/.npm-global",
            "PATH": f"{home}/.npm-global/bin:{home}/.local/bin:" + os.environ.get("PATH", "")}


def agent_env(extra: dict | None = None) -> dict[str, str]:
    """Overrides for the agent's environment (the SDK merges these over os.environ,
    which has already been scrubbed). Blank secrets are a second safety net."""
    env = {k: "" for k in SECRET_KEYS}
    env.update({"CI": "1", "NG_CLI_ANALYTICS": "false"})   # no watch mode, no prompts
    if separated():
        env.update(_user_env(AGENT_USER))
    env.update(proxy_env())
    env.update(extra or {})
    return env


def studio_env(extra: dict | None = None) -> dict[str, str]:
    """Like agent_env, for studio jobs (their own user; never behind the build team's proxy)."""
    env = {k: "" for k in SECRET_KEYS}
    env.update({"CI": "1", "NG_CLI_ANALYTICS": "false"})
    user = studio_user()
    if user:
        env.update(_user_env(user))
    env.update(extra or {})
    return env


def as_agent(cmd: list[str]) -> list[str]:
    """Prefix a command so it runs as the agent user (no-op when not separated)."""
    if not separated():
        return cmd
    env = {**_user_env(AGENT_USER), **proxy_env()}
    return ["runuser", "-u", AGENT_USER, "--", "env", *[f"{k}={v}" for k, v in env.items()], *cmd]


def git(*args: str, cwd: Path | str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """git on the agents' project, always as the agent user."""
    return subprocess.run(as_agent(["git", *args]), cwd=cwd, capture_output=capture, text=True, check=check)


def give_to(user: str | None, *paths: Path | str, skip: Path | None = None) -> None:
    """Hand files to a user (find ! -user …: cheap when they already own them)."""
    if not separated() or not user:
        return
    for p in paths:
        p = Path(p)
        if p.exists():
            prune = ["-path", str(skip), "-prune", "-o"] if skip else []
            subprocess.run(["find", str(p), "-path", "*/node_modules", "-prune", "-o", *prune,
                            "!", "-user", user, "-exec", "chown", "-h", f"{user}:{user}", "{}", "+"],
                           check=False, capture_output=True)


def give_to_agent(*paths: Path | str) -> None:
    """Hand files the bot created (uploads, synced templates) to the agent user."""
    give_to(AGENT_USER, *paths)


def give_to_studio(*paths: Path | str, skip: Path | None = None) -> None:
    give_to(studio_user(), *paths, skip=skip)


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
    for user in dict.fromkeys((AGENT_USER, STUDIO_USER, os.environ.get("LAB_USER", "lab"))):
        try:
            pwd.getpwnam(user)
            out = subprocess.run(["pgrep", "-u", user], capture_output=True, text=True).stdout.split()
            if out:
                subprocess.run(["pkill", "-KILL", "-u", user], capture_output=True)
            total += len(out)
        except (KeyError, FileNotFoundError):
            continue
    return total
