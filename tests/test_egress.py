"""Internet allowlist for the build team; studio jobs as their own user."""
from __future__ import annotations

import asyncio

import egress
import sandbox
from conftest import run


def test_allowlist_matches_hosts_and_subdomains(monkeypatch):
    monkeypatch.setenv("AGENT_EGRESS_ALLOW", "example.org")
    hosts = egress.allowlist()
    assert egress.allowed("api.anthropic.com", hosts) and egress.allowed("registry.npmjs.org", hosts)
    assert egress.allowed("docs.example.org", hosts)
    assert not egress.allowed("evil-anthropic.com", hosts)        # suffix must be a whole label
    assert not egress.allowed("pastebin.com", hosts)
    assert egress.allowed("pypi.org", hosts) and not egress.allowed("upload.pypi.org", hosts)
    assert not egress.allowed("github.com", hosts) and not egress.allowed("api.github.com", hosts)
    assert egress.allowed("codeload.github.com", hosts)


def test_proxy_tunnels_allowed_hosts_and_refuses_others():
    async def go():
        async def echo(r, w):
            w.write(b"hello from upstream")
            await w.drain()
            w.close()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]
        blocked = []
        proxy = egress.EgressProxy(on_block=blocked.append)
        proxy.hosts = ("127.0.0.1",)
        proxy.ports = (str(up_port),)
        await proxy.start(port=0)
        port = proxy.server.sockets[0].getsockname()[1]

        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(200), 5)
        assert b"200 Connection established" in data
        if b"hello" not in data:
            data += await asyncio.wait_for(r.read(200), 5)
        assert b"hello from upstream" in data
        w.close()

        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"CONNECT pastebin.com:443 HTTP/1.1\r\n\r\n")
        await w.drain()
        assert b"403" in await asyncio.wait_for(r.read(100), 5)
        assert blocked == ["pastebin.com"]
        proxy.server.close()
        upstream.close()
    run(go())


def test_only_the_build_team_goes_through_the_proxy(monkeypatch):
    monkeypatch.setenv("AGENT_EGRESS", "restricted")
    if not sandbox.separated():
        return
    env = sandbox.agent_env()
    assert env["HTTPS_PROXY"].startswith("http://127.0.0.1:")
    assert "HTTPS_PROXY" not in sandbox.studio_env()
    assert any(a.startswith("HTTPS_PROXY=") for a in sandbox.as_agent(["true"]))
    monkeypatch.setenv("AGENT_EGRESS", "open")
    assert "HTTPS_PROXY" not in sandbox.agent_env()


def test_studio_jobs_run_as_their_own_user(make_orch):
    import pwd
    o = make_orch()
    if not sandbox.separated() or sandbox.studio_user() == sandbox.agent_user():
        return
    job = o.studio._new_job("commercial-offer", "Client X")
    opts = o.studio._options(job)
    assert opts.user == sandbox.STUDIO_USER
    owner = pwd.getpwuid(__import__("os").stat(job["dir"]).st_uid).pw_name
    assert owner == sandbox.STUDIO_USER
    pm_lab = o.pm_lab_job()["dir"]
    assert pwd.getpwuid(__import__("os").stat(pm_lab).st_uid).pw_name == sandbox.AGENT_USER
