"""Read-only web dashboard (port 8090 inside the container; published on the server's localhost only).

Open it through an SSH tunnel from your laptop:
    ssh -L 8090:localhost:8090 root@YOUR_SERVER   then   http://localhost:8090
Optional extra lock: DASHBOARD_TOKEN in .env, then open http://localhost:8090/?token=<token> once
(a cookie remembers it). It never changes anything: no buttons, no forms.
"""
from __future__ import annotations

import html
import logging
import mimetypes
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

import sandbox

if TYPE_CHECKING:
    from orchestrator import Orchestrator

log = logging.getLogger("dashboard")
e = html.escape

CSS = """
:root { color-scheme: light dark; --bg:#f7f7f5; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e4e4df; --accent:#b91c1c; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --fg:#ecebe6; --muted:#9a9a93; --card:#1e1e1c; --line:#2e2e2b; } }
* { box-sizing: border-box } body { margin:0; font: 15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg) }
header { display:flex; gap:18px; align-items:center; padding:14px 24px; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg) }
header b { font-size:16px } nav a { color:var(--muted); text-decoration:none; margin-right:14px } nav a.on { color:var(--fg); font-weight:600 }
main { max-width:1180px; margin:0 auto; padding:24px } .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px 18px; white-space:pre-wrap; overflow-wrap:anywhere }
.card h3 { margin:0 0 8px; font-size:15px } code { font-size:13px } pre { white-space:pre-wrap; font-size:12.5px; margin:0 }
.shots { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:14px } .shots figure { margin:0 }
.shots img { width:100%; border:1px solid var(--line); border-radius:8px } figcaption { color:var(--muted); font-size:12.5px }
.muted { color:var(--muted) } a { color:var(--accent) }
"""


def _page(title: str, active: str, body: str) -> bytes:
    tabs = [("/", "Status"), ("/score", "Scorecard"), ("/screens", "Screens"), ("/lab", "Lab"), ("/log", "Log")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in tabs)
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<meta http-equiv=refresh content=60><title>{e(title)} · Agent team</title><style>{CSS}</style></head>"
            f"<body><header><b>Agent team</b><nav>{nav}</nav><span class=muted>{time.strftime('%H:%M')}"
            f" · refreshes every minute</span></header><main>{body}</main></body></html>").encode()


class Dashboard:
    def __init__(self, orch: "Orchestrator", port: int = 8090):
        self.o, self.port = orch, port
        self.token = sandbox.secret("DASHBOARD_TOKEN")
        self.httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        dash = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):           # quiet
                pass

            def do_GET(self):
                try:
                    dash.handle(self)
                except Exception as ex:        # a broken page must never take the bot down
                    log.exception("dashboard")
                    self.send_error(500, str(ex)[:200])

        self.httpd = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="dashboard").start()

    # ------------------------------------------------------------------ routing
    def handle(self, req: BaseHTTPRequestHandler) -> None:
        url = urllib.parse.urlparse(req.path)
        q = urllib.parse.parse_qs(url.query)
        cookie_ok = f"bbts_token={self.token}" in (req.headers.get("Cookie") or "")
        if self.token and not cookie_ok and q.get("token", [""])[0] != self.token:
            return self._send(req, 403, b"Forbidden: open /?token=<DASHBOARD_TOKEN> once.", "text/plain")
        extra = {"Set-Cookie": f"bbts_token={self.token}; HttpOnly; SameSite=Strict"} if self.token else {}
        routes = {"/": self.status, "/score": self.score, "/screens": self.screens, "/lab": self.lab, "/log": self.log}
        if url.path == "/img":
            return self.image(req, q.get("p", [""])[0])
        if url.path not in routes:
            return self._send(req, 404, b"Not found", "text/plain")
        self._send(req, 200, routes[url.path](q), "text/html; charset=utf-8", extra)

    def _send(self, req, code, body: bytes, ctype: str, headers: dict | None = None) -> None:
        req.send_response(code)
        req.send_header("Content-Type", ctype)
        req.send_header("Content-Length", str(len(body)))
        req.send_header("X-Content-Type-Options", "nosniff")
        req.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; img-src 'self'")
        for k, v in (headers or {}).items():
            req.send_header(k, v)
        req.end_headers()
        req.wfile.write(body)

    # -------------------------------------------------------------------- pages
    def status(self, q) -> bytes:
        cards = []
        for name in self.o.project_names():
            cards.append(f"<div class=card>{self.o.status(full=True, project=name)}</div>")
        import extras
        try:
            import asyncio
            server = asyncio.run(extras.server(self.o))
        except Exception:
            server = ""
        if server:
            cards.append(f"<div class=card>{server}</div>")
        return _page("Status", "/", "<div class=grid>" + "".join(cards) + "</div>")

    def score(self, q) -> bytes:
        import score
        days = int(q.get("days", ["7"])[0]) if q.get("days", ["7"])[0].isdigit() else 7
        links = " · ".join(f'<a href="/score?days={d}">{d} days</a>' for d in (7, 30, 90))
        return _page("Scorecard", "/score", f"<p>{links}</p><div class=card>{score.report(self.o, min(days, 90))}</div>")

    def _image_link(self, p: Path) -> str:
        return "/img?" + urllib.parse.urlencode({"p": str(p)})

    def screens(self, q) -> bytes:
        out = []
        for name in self.o.project_names():
            folder = self.o.project_root(name)[1] / "design" / "screens"
            shots = sorted(folder.rglob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:24] if folder.is_dir() else []
            changes = []
            try:
                changes = [c for c in self.o.visual.compare(name) if c.image]
            except Exception:
                pass
            if not shots and not changes:
                continue
            out.append(f"<h2>{e(self.o.project_root(name)[0])}</h2>")
            if changes:
                out.append("<h3>Changed vs the approved look</h3><div class=shots>" + "".join(
                    f'<figure><a href="{self._image_link(c.image)}"><img src="{self._image_link(c.image)}" loading=lazy></a>'
                    f"<figcaption>{e(c.name)} · {c.percent:.1f}% changed</figcaption></figure>" for c in changes) + "</div>")
            out.append("<h3>Latest screenshots</h3><div class=shots>" + "".join(
                f'<figure><a href="{self._image_link(p)}"><img src="{self._image_link(p)}" loading=lazy></a>'
                f"<figcaption>{e(p.relative_to(folder).as_posix())}</figcaption></figure>" for p in shots) + "</div>")
        return _page("Screens", "/screens", "".join(out) or "<p class=muted>No screenshots yet.</p>")

    def lab(self, q) -> bytes:
        import lab as lab_mod
        lb = self.o.studio.lab
        dirs = [Path(self.o.pm_lab_job()["dir"])] + [Path(j["dir"]) for j in self.o.state.studio.get("jobs", {}).values()
                                                   if j.get("agent") == "model-lab"]
        rows = "".join(
            f"<tr><td>{'✅' if r['ok'] else '❌'}</td><td>{time.strftime('%d %b %H:%M', time.localtime(r['when']))}</td>"
            f"<td>{e(r['name'])}</td><td>{r['seconds']} s</td><td>{e(r['car'])}</td><td>{e(r['error'][:90])}</td></tr>"
            for r in lab_mod.recent_runs(dirs, 40))
        cur = lb.current
        now = (f"<p>⏳ Now: {e(cur['pipeline'])} · {e(cur['car'])} (run {cur['run']})</p>" if cur else "")
        return _page("Lab", "/lab", f"<div class=card><h3>CarStage lab</h3>Code: {e(lb.version_text())}\n"
                     f"{e(lab_mod.status_text() or 'ready')}</div>{now}<table class=card>{rows}</table>")

    def log(self, q) -> bytes:
        text = self.o.recent_log(200)
        return _page("Log", "/log", f"<div class=card><pre>{e(text)}</pre></div>")

    def image(self, req, raw: str) -> None:
        """Only PNG/JPG files inside the projects or the bot's visual-diff folder."""
        p = Path(raw).resolve()
        roots = [self.o.cfg.master.resolve(), (self.o.cfg.data_dir / "visual").resolve()]
        if (not any(r in p.parents for r in roots) or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp")
                or not p.is_file()):
            return self._send(req, 404, b"Not found", "text/plain")
        self._send(req, 200, p.read_bytes(), mimetypes.guess_type(p.name)[0] or "image/png",
                   {"Cache-Control": "max-age=60"})
