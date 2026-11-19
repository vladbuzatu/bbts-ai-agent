"""The agents' browser really works in the image (CI, as the agent user):
  docker run --rm -u agent -w /app --entrypoint python bbts-agents:ci tests/browser_check.py

Starts Playwright MCP exactly as the bot configures it (orchestrator.browser_server), serves a small
page locally, and over MCP's stdio JSON-RPC: navigates to it, reads the page, sees a console error and
saves a screenshot. Catches a Chromium/Playwright-MCP version mismatch, a missing binary or a
sandbox problem before it reaches the server.
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
import orchestrator  # noqa: E402

PAGE = b"""<!doctype html><html><head><title>Preview check</title></head>
<body><h1>Hello from the preview</h1><button onclick="this.textContent='Clicked'">Press me</button>
<script>console.error('deliberate test error')</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    out = Path(tempfile.mkdtemp(prefix="browser-check-"))
    cfg = orchestrator.browser_server(out)
    if not cfg:
        print("FAIL: browser_server() returned None (playwright-mcp or Chromium missing)")
        return 1
    print("starting:", cfg["command"], " ".join(cfg["args"]))
    p = subprocess.Popen([cfg["command"], *cfg["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=sys.stderr, text=True, cwd=out)
    n = 0

    def call(method, params=None, notify=False):
        nonlocal n
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            n += 1
            msg["id"] = n
        p.stdin.write(json.dumps(msg) + "\n")
        p.stdin.flush()
        if notify:
            return None
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("playwright-mcp exited")
            resp = json.loads(line)
            if resp.get("id") == n:
                if "error" in resp:
                    raise RuntimeError(f"{method}: {resp['error']}")
                return resp["result"]

    def text(result):
        return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")

    try:
        call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "browser-check", "version": "1"}})
        call("notifications/initialized", notify=True)
        tools = {t["name"] for t in call("tools/list")["tools"]}
        need = {"browser_navigate", "browser_snapshot", "browser_click", "browser_take_screenshot",
                "browser_console_messages"}
        assert need <= tools, f"missing tools: {need - tools}"
        nav = text(call("tools/call", {"name": "browser_navigate", "arguments": {"url": url}}))
        assert "Hello from the preview" in nav or "Preview check" in nav, nav[:2000]
        snap = text(call("tools/call", {"name": "browser_snapshot", "arguments": {}}))
        assert "Press me" in snap, snap[:2000]
        console = text(call("tools/call", {"name": "browser_console_messages", "arguments": {}}))
        assert "deliberate test error" in console, console[:2000]
        call("tools/call", {"name": "browser_take_screenshot", "arguments": {"filename": "home.png"}})
        shots = [f for f in out.rglob("*.png") if f.stat().st_size > 1000]
        assert shots, f"no screenshot in {out}: {list(out.rglob('*'))}"
        print(f"OK: navigated, read the page, saw the console error, screenshot {shots[0].name} "
              f"({shots[0].stat().st_size} bytes) as user {os.getuid()}")
        return 0
    finally:
        p.kill()
        srv.shutdown()


if __name__ == "__main__":
    sys.exit(main())
