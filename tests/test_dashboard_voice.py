"""Read-only dashboard and voice transcription."""
from __future__ import annotations

import json
import urllib.request

import pytest


def get(port, path, cookie=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Cookie": cookie} if cookie else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(errors="replace"), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), e.headers


def test_dashboard_pages_render_and_images_stay_inside(make_orch, monkeypatch):
    import dashboard
    o = make_orch(multi=True)
    shot = o.cfg.workspace / "design" / "screens" / "home.png"
    shot.parent.mkdir(parents=True)
    from PIL import Image
    Image.new("RGB", (10, 10), "white").save(shot)
    d = dashboard.Dashboard(o, port=0)
    d.start()
    port = d.httpd.server_address[1]
    for path in ("/", "/score", "/screens", "/lab", "/log"):
        code, body, _ = get(port, path)
        assert code == 200 and "Agent team" in body, path
    code, body, _ = get(port, "/screens")
    assert "home.png" in body
    import urllib.parse
    assert get(port, "/img?" + urllib.parse.urlencode({"p": str(shot)}))[0] == 200
    assert get(port, "/img?" + urllib.parse.urlencode({"p": "/etc/passwd"}))[0] == 404
    assert get(port, "/img?" + urllib.parse.urlencode({"p": str(o.cfg.data_dir / "state.json")}))[0] == 404
    d.httpd.shutdown()


def test_dashboard_token(make_orch, monkeypatch):
    import dashboard
    import sandbox
    monkeypatch.setitem(sandbox._secrets, "DASHBOARD_TOKEN", "s3cret")
    o = make_orch(multi=True)
    d = dashboard.Dashboard(o, port=0)
    d.start()
    port = d.httpd.server_address[1]
    assert get(port, "/")[0] == 403
    code, _, headers = get(port, "/?token=s3cret")
    assert code == 200 and "bbts_token=s3cret" in headers.get("Set-Cookie", "")
    assert get(port, "/log", cookie="bbts_token=s3cret")[0] == 200
    d.httpd.shutdown()


def test_voice_transcription_request(monkeypatch):
    import sandbox
    import voice
    monkeypatch.setitem(sandbox._secrets, "VOICE_OPENAI_API_KEY", "sk-voice-test")
    monkeypatch.setenv("VOICE_LANGUAGE", "ro")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"text": "Pornește build-ul pentru carstage"}).encode()

    def fake_urlopen(req, timeout=0):
        seen["auth"] = req.headers["Authorization"]
        seen["body"] = req.data
        return Resp()
    monkeypatch.setattr(voice.urllib.request, "urlopen", fake_urlopen)
    text = voice.transcribe(b"OggS fake audio", ".ogg")
    assert text == "Pornește build-ul pentru carstage"
    assert seen["auth"] == "Bearer sk-voice-test"
    assert b'name="language"\r\n\r\nro' in seen["body"] and b"gpt-4o-transcribe" in seen["body"]


def test_voice_without_key_explains(monkeypatch):
    import sandbox
    import voice
    monkeypatch.delitem(sandbox._secrets, "VOICE_OPENAI_API_KEY", raising=False)
    monkeypatch.delitem(sandbox._secrets, "LAB_OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="VOICE_OPENAI_API_KEY"):
        voice.transcribe(b"x")
