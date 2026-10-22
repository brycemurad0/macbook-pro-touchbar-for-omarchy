#!/usr/bin/env python3
"""Wiring B bridge: serve gemini-3.8-flash to the ElevenLabs agent as a
custom LLM, OpenAI-compatible, so the conversation brain is ours (own
Google credential, own model choice) while ElevenLabs keeps ears,
turn-taking, and mouth.

Listens on 127.0.0.1:8911 only; exposed to ElevenLabs through Tailscale
Funnel. Every request must carry the shared bridge secret (generated
into voice-secrets.env on first run, also stored as an ElevenLabs
workspace secret by apply_agent.py). Forwards to the Gemini API's
OpenAI-compatible endpoint with GEMINI_API_KEY from voice-secrets.env.
Gemini 3.8 rejects legacy sampling params, so temperature/top_p/top_k
are stripped; thinking stays at the model's default (medium = snappy).
"""
import json
import os
import secrets as secrets_mod
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.expanduser("~/.config/omarchy/touchbar")
SECRETS_ENV = os.path.join(BASE, "voice-secrets.env")
PORT = 8911
MODEL = os.environ.get("LLM_BRIDGE_MODEL", "gemini-3.8-flash")
UPSTREAM = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def _read_secrets():
    out = {}
    try:
        for line in open(SECRETS_ENV):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    except OSError:
        pass
    return out


def bridge_secret():
    sec = _read_secrets()
    if sec.get("LLM_BRIDGE_SECRET"):
        return sec["LLM_BRIDGE_SECRET"]
    value = secrets_mod.token_urlsafe(32)
    with open(SECRETS_ENV, "a") as f:
        f.write(f"LLM_BRIDGE_SECRET={value}\n")
    os.chmod(SECRETS_ENV, 0o600)
    print("generated LLM_BRIDGE_SECRET into voice-secrets.env", flush=True)
    return value


def gemini_key():
    return os.environ.get("GEMINI_API_KEY") or _read_secrets().get(
        "GEMINI_API_KEY")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True, "model": MODEL,
                             "key": bool(gemini_key())})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions",
                                         "/chat/completions"):
            return self._json(404, {"error": "not found"})
        if self.headers.get("Authorization") != f"Bearer {SECRET}":
            return self._json(401, {"error": "unauthorized"})
        key = gemini_key()
        if not key:
            return self._json(503, {"error": "GEMINI_API_KEY not configured"})
        try:
            req_body = json.loads(self.rfile.read(
                int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json(400, {"error": "bad json"})
        # Gemini 3.8 errors on legacy sampling params; the persona, not
        # temperature, shapes replies
        for k in ("temperature", "top_p", "top_k", "presence_penalty",
                  "frequency_penalty"):
            req_body.pop(k, None)
        req_body["model"] = MODEL
        stream = bool(req_body.get("stream"))
        tools = len(req_body.get("tools") or [])
        print(f"chat completion: stream={stream} tools={tools} "
              f"msgs={len(req_body.get('messages') or [])}", flush=True)
        up = urllib.request.Request(
            UPSTREAM, data=json.dumps(req_body).encode(), method="POST",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            resp = urllib.request.urlopen(up, timeout=120)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            print(f"upstream HTTP {exc.code}: {detail}", flush=True)
            return self._json(exc.code, {"error": f"upstream: {detail}"})
        except Exception as exc:
            print(f"upstream failed: {exc}", flush=True)
            return self._json(502, {"error": f"upstream: {exc}"})
        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                print("client closed stream early", flush=True)
        else:
            body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


SECRET = bridge_secret()


def main():
    if not gemini_key():
        print("warning: GEMINI_API_KEY not set (voice-secrets.env); "
              "requests will 503 until it is added", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"llm bridge on 127.0.0.1:{PORT} model={MODEL}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
