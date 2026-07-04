#!/usr/bin/env python3
"""
SCOUT Companion Server — exposes SCOUT (Ollama) as an OpenAI-compatible API
for any VS Code AI extension (Continue, Cline, Roo Code, etc.).

VS Code extension config:
  API Base: http://localhost:11435/v1
  API Key:  any string (e.g. "scout")
  Model:    scout / scout-mem / any installed Ollama model

Run:
  python3 ~/scout/server.py            # foreground
  python3 ~/scout/server.py --bg       # background with logs at ~/scout/server.log
"""

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Reuse the bridge's tool implementations
sys.path.insert(0, str(Path(__file__).parent))
from bridge import (
    TOOL_DISPATCH, TOOL_SCHEMAS, MODEL,
    OLLAMA_URL, load_system_prompt,
)

import requests

PORT = int(os.environ.get("SCOUT_PORT", "11435"))
HOST = os.environ.get("SCOUT_HOST", "127.0.0.1")


# ---------------------------------------------------------------------------
# OpenAI-compatible tool schema (different shape from Ollama's)
# ---------------------------------------------------------------------------

def to_openai_tools():
    out = []
    for t in TOOL_SCHEMAS:
        f = t["function"]
        out.append({
            "type": "function",
            "function": {
                "name": f["name"],
                "description": f["description"],
                "parameters": f["parameters"],
            },
        })
    return out


# ---------------------------------------------------------------------------
# Ollama call — but with OpenAI-style tool_calls in the response shape
# ---------------------------------------------------------------------------

def call_ollama_with_tools(model: str, messages: list) -> dict:
    """Call Ollama, return message in OpenAI shape (with tool_calls if any)."""
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "stream": False,
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=300)
    r.raise_for_status()
    data = r.json()
    msg = data.get("message", {})

    out = {"role": "assistant", "content": msg.get("content", "") or ""}
    if msg.get("tool_calls"):
        out["tool_calls"] = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc["function"]["name"],
                    "arguments": json.dumps(tc["function"].get("arguments", {})),
                },
            }
            for i, tc in enumerate(msg["tool_calls"])
        ]
    return out


# ---------------------------------------------------------------------------
# Agent loop — drives tool calls until model returns a final answer
# ---------------------------------------------------------------------------

def run_agent(model: str, messages: list, max_iters: int = 10) -> str:
    sys_prompt = load_system_prompt()
    if not messages or messages[0].get("role") != "system":
        messages = [{"role": "system", "content": sys_prompt}] + messages
    else:
        messages[0]["content"] = sys_prompt  # always inject latest

    for _ in range(max_iters):
        msg = call_ollama_with_tools(model, messages)
        messages.append(msg)
        if not msg.get("tool_calls"):
            return msg.get("content", "")
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            impl = TOOL_DISPATCH.get(name)
            try:
                result = impl(args) if impl else f"unknown tool: {name}"
            except Exception as e:
                result = f"error: {e}"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": str(result)[:16000],
            })
    return "(tool loop exceeded max iterations)"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[scout] {self.address_string()} {fmt % args}\n")

    def _json(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/v1/models":
            self._json(200, {
                "object": "list",
                "data": [
                    {"id": "scout", "object": "model", "owned_by": "scout"},
                    {"id": "scout-mem", "object": "model", "owned_by": "scout"},
                ],
            })
        elif self.path == "/health":
            self._json(200, {"status": "ok", "model": MODEL})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return

        body = self._read_body()
        model = body.get("model", MODEL)
        messages = body.get("messages", [])
        stream = body.get("stream", False)

        try:
            content = run_agent(model, messages)
        except Exception as e:
            self._json(500, {"error": str(e)})
            return

        resp = {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

        if stream:
            # Minimal SSE — most VS Code extensions also accept non-streaming
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = json.dumps(resp)
            self.wfile.write(f"data: {chunk}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._json(200, resp)


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bg", action="store_true", help="run in background")
    args = ap.parse_args()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"SCOUT server on http://{HOST}:{PORT}/v1 (model={MODEL})", file=sys.stderr)

    if args.bg:
        log_path = Path.home() / "scout" / "server.log"
        pid_path = Path.home() / "scout" / "server.pid"
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text().strip()), 0)
                print("already running", file=sys.stderr)
                return
            except OSError:
                pass
        pid = os.fork()
        if pid > 0:
            print(f"started pid={pid}, log={log_path}", file=sys.stderr)
            return
        os.setsid()
        with open(log_path, "ab", 0) as logf:
            os.dup2(logf.fileno(), sys.stdout.fileno())
            os.dup2(logf.fileno(), sys.stderr.fileno())
        pid_path.write_text(str(os.getpid()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()