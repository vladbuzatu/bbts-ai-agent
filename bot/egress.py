"""Internet allowlist for the build team (AGENT_EGRESS=restricted).

- A small HTTP(S) proxy in the bot (127.0.0.1:3128) only connects to allowlisted hosts: Claude's API,
  package registries, GitHub, and documentation sites. Agents get HTTPS_PROXY pointing at it.
- A firewall rule (iptables owner match) rejects everything else the agent user tries to send out,
  so a prompt-injected agent can't simply ignore the proxy. Needs the NET_ADMIN capability
  (docker-compose.yml) and only applies to the agent user: studio jobs (website research) run as
  their own user with normal access, the bot itself is unaffected.

Add hosts with AGENT_EGRESS_ALLOW=example.com,docs.example.org (a host also allows its subdomains).
"""
from __future__ import annotations

import asyncio
import logging
import os
import pwd
import subprocess
import time

import sandbox

log = logging.getLogger("egress")

DEFAULT_ALLOW = (
    # Claude
    "anthropic.com", "claude.ai", "claude.com",
    # packages
    "registry.npmjs.org", "npmjs.org", "npmjs.com", "pypi.org", "pythonhosted.org", "nodejs.org",
    # code
    "github.com", "githubusercontent.com",
    # documentation the playbooks point to
    "angular.dev", "angular.io", "djangoproject.com", "django-rest-framework.org", "developer.mozilla.org",
    "web.dev", "w3.org", "owasp.org", "python.org", "typescriptlang.org", "playwright.dev", "vitest.dev",
    "rxjs.dev", "pydantic.dev", "docs.pytest.org", "readthedocs.io",
    # fonts and icons for designs
    "fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net", "unpkg.com",
    # browser downloads for Playwright
    "playwright.azureedge.net", "playwright-akamai.azureedge.net", "playwright-verizon.azureedge.net",
    "cdn.playwright.dev",
)
CHAIN = "BBTS_AGENT_EGRESS"


def allowlist() -> tuple[str, ...]:
    extra = tuple(h.strip().lower().lstrip(".") for h in os.environ.get("AGENT_EGRESS_ALLOW", "").split(",") if h.strip())
    return DEFAULT_ALLOW + extra


def allowed(host: str, hosts: tuple[str, ...] | None = None) -> bool:
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in (hosts or allowlist()))


class EgressProxy:
    def __init__(self, on_block=None):
        self.hosts = allowlist()
        self.ports = ("443", "8443")
        self.blocked: dict[str, float] = {}       # host -> last time (for /server and the log)
        self.on_block = on_block
        self.server: asyncio.AbstractServer | None = None

    async def start(self, port: int = sandbox.PROXY_PORT) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", port)

    def _deny(self, host: str) -> None:
        first = host not in self.blocked or time.time() - self.blocked[host] > 3600
        self.blocked[host] = time.time()
        if first and self.on_block:
            self.on_block(host)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=20)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3:
            writer.close()
            return
        method, target, version = parts
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            host = host.strip("[]")
            if port not in self.ports or not allowed(host, self.hosts):
                self._deny(host)
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=20)
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
        elif target.startswith("http://"):
            rest = target[len("http://"):]
            hostport, _, path = rest.partition("/")
            host, _, port = hostport.partition(":")
            if not allowed(host, self.hosts):
                self._deny(host)
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            try:
                up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port or 80)), timeout=20)
            except (OSError, asyncio.TimeoutError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            up_w.write(f"{method} /{path} {version}\r\n".encode() + head.split(b"\r\n", 1)[1])
        else:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer), return_exceptions=True)


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    finally:
        try:
            dst.close()
        except Exception:
            pass


def install_firewall() -> str | None:
    """Reject everything the agent user sends that isn't to localhost (the proxy, dev servers).
    Returns None on success, else what went wrong."""
    if not sandbox.egress_restricted():
        return None
    try:
        uid = str(pwd.getpwnam(sandbox.AGENT_USER).pw_uid)
    except KeyError:
        return "no agent user"
    for tool in ("iptables", "ip6tables"):
        cmds = [
            [tool, "-N", CHAIN],
            [tool, "-F", CHAIN],
            [tool, "-A", CHAIN, "-o", "lo", "-j", "ACCEPT"],
            [tool, "-A", CHAIN, "-j", "REJECT"],
        ]
        for c in cmds:
            subprocess.run(c, capture_output=True)
        check = subprocess.run([tool, "-C", "OUTPUT", "-m", "owner", "--uid-owner", uid, "-j", CHAIN], capture_output=True)
        if check.returncode != 0:
            r = subprocess.run([tool, "-I", "OUTPUT", "1", "-m", "owner", "--uid-owner", uid, "-j", CHAIN],
                               capture_output=True, text=True)
            if r.returncode != 0 and tool == "iptables":
                return f"{tool}: {r.stderr.strip()[:200] or 'failed'} (the container needs cap_add: NET_ADMIN)"
            # ip6tables may be unavailable when the container has no IPv6 at all: then there's no IPv6 route either
    return None
