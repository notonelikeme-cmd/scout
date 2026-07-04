# SCOUT — Autonomous Vulnerability Research Agent

Local Ollama-based agent wired into VS Code AI extensions and a custom MCP
server for smart contract + web2 security research.

## Quick start

```bash
# 1. start the OpenAI-compat server (background)
python3 ~/scout/server.py --bg

# 2. point your VS Code AI extension at it
#    Continue:  http://127.0.0.1:11435/v1
#    Cline/Roo: http://127.0.0.1:11435/v1, model "scout-mem"
#    API key:   any string (e.g. "scout")

# 3. or use the CLI directly
python3 ~/scout/bridge.py "audit 0xABC... on Ethereum"

# 4. or run a full Polymarket audit pipeline
python3 ~/scout/polymarket_orchestrator.py 0xABC...

# 5. or use the MCP server (when an MCP client is wired in)
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 ~/scout/mcp_server.py
```

## Files

| File | Purpose |
|---|---|
| `SYSTEM_PROMPT.md` | The reference system prompt (LM Studio / manual) |
| `VSCODE_SYSTEM_PROMPT.md` | Same prompt, paste-friendly for VS Code extensions |
| `Modelfile` | Builds `scout` Ollama model (qwen2.5-coder:14b base) |
| `Modelfile.mem` | Builds `scout-mem` (all 3 base models + memory schema) |
| `bridge.py` | Tool implementations (file, shell, memory) — REPL entry point |
| `server.py` | OpenAI-compat HTTP server — VS Code extensions point here |
| `mcp_server.py` | Model Context Protocol server — exposes 8 security tools |
| `mcp-manifest.json` | MCP server manifest for registry publication |
| `polymarket_orchestrator.py` | 5-phase audit pipeline for Polymarket targets |
| `polymarket/SYSTEM_PROMPT.md` | Polymarket-specific agent prompt |
| `polymarket/AGENT_BRIEFINGS.md` | 6 sub-agent role briefings |
| `polymarket/Modelfile.pm` | Builds `scout-pm` (Polymarket specialized) |
| `RESEARCH_2026-06.md` | Verified tech landscape as of June 2026 |
| `memory/` | Persistent state (user, environment, session JSON) |

## Ollama models

| Model | Base | Size | Use |
|---|---|---|---|
| `scout` | qwen2.5-coder:14b | 9 GB | Code generation, structured output |
| `scout-mem` | qwen2.5-coder + deepseek-r1 + gemma4 | 9.6 GB | Memory + tools, multi-task |
| `scout-pm` | qwen2.5-coder + Polymarket prompt | 9 GB | Polymarket-specific audits |

Switch the server's model:
```bash
kill $(cat ~/scout/server.pid)
SCOUT_MODEL=scout-pm python3 ~/scout/server.py --bg
```

## MCP server

8 tools exposed:

- `scout_recon` — subdomain enum + contract discovery
- `scout_scan_solidity` — slither + aderyn
- `scout_fuzz` — echidna or medusa
- `scout_cast_call` — read-only on-chain calls (refuses state-changing methods)
- `scout_decompile` — heimdall-rs bytecode decompilation
- `scout_search_findings` — local finding store search
- `scout_triage` — Polymarket severity scoring
- `scout_draft_report` — markdown report generation

Wire into Continue (`~/.continue/config.json`):
```json
{
  "mcpServers": [{
    "name": "scout",
    "command": "python3",
    "args": ["~/scout/mcp_server.py"]
  }]
}
```

## Toolchain dependencies

Required for the security tools to work:

```bash
brew install foundry    # forge, cast, anvil
pipx install slither-analyzer
pipx install aderyn
# Optional:
pipx install eth-security-toolbox
```

## Polymarket audit pipeline

```bash
# Full automated pipeline
python3 ~/scout/polymarket_orchestrator.py 0x4d97fc1d4d8b8b9b48f9e5d6c2a1b3f4e5d6c7a8b

# Or just the orchestrator with a web2 target
python3 ~/scout/polymarket_orchestrator.py --target https://polymarket.com

# Resume an interrupted session
python3 ~/scout/polymarket_orchestrator.py --resume 1734567890-a1b2c3

# Start from a specific phase
python3 ~/scout/polymarket_orchestrator.py --resume <id> --phase 4
```

Output goes to `/tmp/scout/sessions/<id>/`.

## State persistence

Three memory files in `~/scout/memory/`:

- `user.json` — operator profile (handle, platforms, preferences)
- `environment.json` — local setup (tools, paths, models)
- `session.json` — active target, scope, findings_so_far

Reset:
```bash
rm ~/scout/memory/*.json
```

## Server management

```bash
# start
python3 ~/scout/server.py --bg

# status
curl http://127.0.0.1:11435/health

# stop
kill $(cat ~/scout/server.pid)

# logs
tail -f ~/scout/server.log
```

## Known limitations

- 14B-class models are not reliable for novel exploit reasoning; for Critical-class
  findings, always verify manually
- Local models don't have web access; web2 recon is constrained to local tooling
- No memory across server restarts (until you commit to writing to memory)
- No parallel sub-agent execution; sequential tool calls per turn
- MCP server doesn't auto-publish to registry yet (you have to push `mcp-manifest.json`)

## Roadmap

- Per-agent role-specific Ollama models (scout-coder, scout-reasoner, scout-comm)
- Memory bridge to Hermes Agent's memory schema
- Auto-publish to MCP registry
- Real parallel sub-agent fan-out via asyncio
- Heuristic-driven static analysis (custom detectors)