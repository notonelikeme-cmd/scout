#!/usr/bin/env python3
"""
SCOUT Bridge — gives SCOUT Ollama models actual file + terminal + memory tools.

Run:
  python3 ~/scout/bridge.py                              # interactive REPL
  python3 ~/scout/bridge.py "audit contract 0x... on ETH" # one-shot

The model thinks it has tools. We provide them. The model invokes them via
Ollama's tool-calling API. We execute, feed results back, loop until done.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = os.environ.get("SCOUT_MODEL", "scout-mem")
MEMORY_DIR = Path.home() / "scout" / "memory"
WORKSPACE = Path.home() / "scout"
SESSION_DIR = Path("/tmp/scout/sessions")

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------------

def t_read_file(args: dict) -> str:
    path = os.path.expanduser(args["path"])
    if not os.path.isabs(path):
        path = str(WORKSPACE / path)
    try:
        with open(path, "r", errors="replace") as f:
            content = f.read()
        if args.get("offset"):
            content = "\n".join(content.split("\n")[args["offset"]:])
        if args.get("limit"):
            lines = content.split("\n")[: args["limit"]]
            content = "\n".join(lines)
        return content or "(empty file)"
    except FileNotFoundError:
        return f"ERROR: file not found: {path}"
    except Exception as e:
        return f"ERROR: {e}"


def t_write_file(args: dict) -> str:
    path = os.path.expanduser(args["path"])
    if not os.path.isabs(path):
        path = str(WORKSPACE / path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(args["content"])
        return f"OK: wrote {len(args['content'])} bytes to {path}"
    except Exception as e:
        return f"ERROR: {e}"


def t_list_dir(args: dict) -> str:
    path = os.path.expanduser(args.get("path", "."))
    if not os.path.isabs(path):
        path = str(WORKSPACE / path)
    try:
        entries = sorted(os.listdir(path))
        out = []
        for e in entries[:200]:
            full = os.path.join(path, e)
            tag = "/" if os.path.isdir(full) else ""
            out.append(f"{e}{tag}")
        return "\n".join(out) or "(empty directory)"
    except Exception as e:
        return f"ERROR: {e}"


def t_search_files(args: dict) -> str:
    """Grep-style content search across files."""
    pattern = args["pattern"]
    path = os.path.expanduser(args.get("path", "."))
    if not os.path.isabs(path):
        path = str(WORKSPACE / path)
    glob = args.get("glob", "*")
    try:
        result = subprocess.run(
            ["rg", "--no-heading", "-n", "--max-count", "20",
             "-g", glob, pattern, path],
            capture_output=True, text=True, timeout=30,
        )
        return result.stdout or "(no matches)"
    except FileNotFoundError:
        # fall back to grep
        try:
            result = subprocess.run(
                ["grep", "-rn", "--include", glob, pattern, path],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout or "(no matches)"
        except Exception as e:
            return f"ERROR: {e}"
    except Exception as e:
        return f"ERROR: {e}"


def t_run_command(args: dict) -> str:
    """Run a shell command. Read-only commands preferred. 60s timeout."""
    cmd = args["command"]
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=os.path.expanduser(args.get("cwd", str(Path.home()))),
        )
        out = (result.stdout or "") + (result.stderr or "")
        if len(out) > 8000:
            out = out[:4000] + "\n...[truncated]...\n" + out[-4000:]
        return f"exit_code={result.returncode}\n{out}" or "(no output)"
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 60s"
    except Exception as e:
        return f"ERROR: {e}"


def t_read_memory(args: dict) -> str:
    target = args.get("target", "session")
    path = MEMORY_DIR / f"{target}.json"
    if not path.exists():
        return f"(empty — no {target} memory yet)"
    try:
        return path.read_text()
    except Exception as e:
        return f"ERROR: {e}"


def t_write_memory(args: dict) -> str:
    target = args.get("target", "session")
    updates = args.get("updates", {})
    path = MEMORY_DIR / f"{target}.json"
    try:
        existing = json.loads(path.read_text()) if path.exists() else {}
        # shallow merge — top-level keys replaced
        if isinstance(existing, dict) and isinstance(updates, dict):
            existing.update(updates)
            merged = existing
        else:
            merged = updates
        path.write_text(json.dumps(merged, indent=2))
        return f"OK: updated {target} memory"
    except Exception as e:
        return f"ERROR: {e}"


TOOL_DISPATCH = {
    "read_file": t_read_file,
    "write_file": t_write_file,
    "list_dir": t_list_dir,
    "search_files": t_search_files,
    "run_command": t_run_command,
    "read_memory": t_read_memory,
    "write_memory": t_write_memory,
}

# ----------------------------------------------------------------------------
# Tool schemas (sent to Ollama)
# ----------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a local file. Pass absolute path or path relative to ~/scout/. Use offset/limit for large files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path like ~/scout/file.md or /Users/nova/.../contract.sol"},
                    "offset": {"type": "integer", "description": "Line number to start from (0-indexed)"},
                    "limit": {"type": "integer", "description": "Max lines to return"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a local file. Creates parent directories. OVERWRITES existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files in a directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Grep for a pattern across files. Returns matching lines with file paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Directory to search in"},
                    "glob": {"type": "string", "description": "File glob filter, e.g. '*.sol'"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command. Read-only commands strongly preferred. Examples: 'ls', 'cat file', 'forge build', 'slither .', 'cast call 0x... balanceOf(address) 0x...'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "Read persistent memory. Targets: 'user' (operator profile), 'environment' (local setup), 'session' (current task state).",
            "parameters": {
                "type": "object",
                "properties": {"target": {"type": "string", "enum": ["user", "environment", "session"]}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_memory",
            "description": "Write to persistent memory. Pass a dict of keys/values to merge into the target.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": ["user", "environment", "session"]},
                    "updates": {"type": "object"},
                },
                "required": ["target", "updates"],
            },
        },
    },
]

# ----------------------------------------------------------------------------
# Agent loop
# ----------------------------------------------------------------------------

SYSTEM_PROMPT_PATH = Path.home() / "scout" / "SYSTEM_PROMPT.md"


def load_system_prompt() -> str:
    if SYSTEM_PROMPT_PATH.exists():
        return SYSTEM_PROMPT_PATH.read_text()
    return "You are SCOUT, a vulnerability research assistant. Use your tools to help the operator."


def call_ollama(messages: list) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
        "stream": False,
    }
    r = requests.post(OLLAMA_URL, json=payload, timeout=300)
    r.raise_for_status()
    return r.json()


def run_turn(messages: list, max_tool_calls: int = 10) -> str:
    """Run one full turn — agent may invoke multiple tools before answering."""
    tool_calls_made = 0
    while tool_calls_made < max_tool_calls:
        resp = call_ollama(messages)
        msg = resp.get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            # Final answer
            return msg.get("content", "")

        # Execute each tool call, append results
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            print(f"  → {name}({json.dumps(args)[:120]})", file=sys.stderr)

            impl = TOOL_DISPATCH.get(name)
            if not impl:
                result = f"ERROR: unknown tool {name}"
            else:
                try:
                    result = impl(args)
                except Exception as e:
                    result = f"ERROR executing {name}: {e}"

            messages.append({
                "role": "tool",
                "content": str(result)[:16000],
            })
            tool_calls_made += 1

    return "(tool call limit reached — agent looped too long, aborting)"


def main():
    system_prompt = load_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]

    if len(sys.argv) > 1:
        # one-shot mode
        user_msg = " ".join(sys.argv[1:])
        messages.append({"role": "user", "content": user_msg})
        answer = run_turn(messages)
        print(answer)
        return

    # REPL mode
    print(f"SCOUT online (model={MODEL}). Type 'exit' to quit.", file=sys.stderr)
    while True:
        try:
            user_msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye", file=sys.stderr)
            break
        if not user_msg:
            continue
        if user_msg.lower() in {"exit", "quit"}:
            break
        messages.append({"role": "user", "content": user_msg})
        answer = run_turn(messages)
        print(f"\nscout>\n{answer}\n")


if __name__ == "__main__":
    main()