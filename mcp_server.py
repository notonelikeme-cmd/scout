#!/usr/bin/env python3
"""
SCOUT MCP Server — exposes SCOUT security tools via Model Context Protocol.

Run standalone for testing:
  python3 ~/scout/mcp_server.py

Wire into MCP clients (Continue, Cline, Roo Code) via the manifest at
~/scout/mcp-manifest.json.

Tools exposed (see mcp-manifest.json for full descriptions):
  scout_recon, scout_scan_solidity, scout_fuzz, scout_cast_call,
  scout_decompile, scout_search_findings, scout_triage, scout_draft_report
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

WORKSPACE = Path.home() / "scout"
MEMORY_DIR = WORKSPACE / "memory"
ARTIFACT_DIR = Path("/tmp/scout/sessions")

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Tool implementations (read-only / safe-by-default)
# ---------------------------------------------------------------------------

def scout_recon(args: dict) -> str:
    """Recon: subdomain enum + endpoint discovery for web2, contract discovery for web3."""
    target = args.get("target", "")
    scope = args.get("scope", "passive")
    out = f"=== RECON: {target} (scope={scope}) ===\n"
    if not target:
        return out + "ERROR: target required"

    # Web2: subdomain enum
    try:
        r = subprocess.run(
            ["subfinder", "-d", target, "-silent", "-all"],
            capture_output=True, text=True, timeout=120,
        )
        subdomains = r.stdout.strip().split("\n") if r.stdout else []
    except FileNotFoundError:
        subdomains = ["[subfinder not installed]"]
    except subprocess.TimeoutExpired:
        subdomains = ["[subfinder timed out]"]

    out += f"\n[web2] Subdomains found: {len(subdomains)}"
    for s in subdomains[:20]:
        out += f"\n  - {s}"
    if len(subdomains) > 20:
        out += f"\n  ... and {len(subdomains) - 20} more"

    # Web3: if it looks like an address, try cast
    if target.startswith("0x") and len(target) == 42:
        out += f"\n\n[web3] Resolving contract {target}..."
        try:
            r = subprocess.run(
                ["cast", "code", target, "--rpc-url", os.environ.get("ETH_RPC", "https://eth.llamarpc.com")],
                capture_output=True, text=True, timeout=30,
            )
            code = r.stdout.strip()
            out += f"\n  bytecode length: {len(code) // 2} bytes"
            out += f"\n  verified: check https://etherscan.io/address/{target}#code"
        except Exception as e:
            out += f"\n  ERROR: {e}"

    return out


def scout_scan_solidity(args: dict) -> str:
    """Run slither and aderyn on a Solidity project. Returns findings as JSON."""
    path = args.get("path", ".")
    detectors = args.get("detectors", "medium,high,critical")
    out = f"=== SOLIDITY STATIC ANALYSIS: {path} ===\n"

    findings = []

    # Slither
    try:
        r = subprocess.run(
            ["slither", path, "--json", "-", f"--filter={detectors}"],
            capture_output=True, text=True, timeout=300,
        )
        if r.stdout:
            try:
                slither_data = json.loads(r.stdout)
                if not slither_data.get("success", True) and slither_data.get("results"):
                    for det in slither_data["results"].get("detectors", []):
                        findings.append({
                            "source": "slither",
                            "check": det.get("check"),
                            "impact": det.get("impact"),
                            "confidence": det.get("confidence"),
                            "description": det.get("description", "")[:500],
                            "file": (det.get("elements") or [{}])[0].get("source_mapping", {}).get("filename_relative"),
                            "line": (det.get("elements") or [{}])[0].get("source_mapping", {}).get("lines", [None])[0],
                        })
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        out += "\n[slither not installed — skipping]"
    except subprocess.TimeoutExpired:
        out += "\n[slither timed out after 5min]"

    # Aderyn
    try:
        r = subprocess.run(
            ["aderyn", path, "--json"],
            capture_output=True, text=True, timeout=300,
        )
        if r.stdout:
            try:
                aderyn_data = json.loads(r.stdout)
                for issue in aderyn_data.get("high_issues", []) + aderyn_data.get("low_issues", []):
                    findings.append({
                        "source": "aderyn",
                        "check": issue.get("title"),
                        "impact": issue.get("severity"),
                        "description": issue.get("description", "")[:500],
                        "file": issue.get("file_path"),
                        "line": (issue.get("line_numbers") or [None])[0],
                    })
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        out += "\n[aderyn not installed — skipping]"
    except subprocess.TimeoutExpired:
        out += "\n[aderyn timed out]"

    out += f"\nTotal findings: {len(findings)}\n"
    out += json.dumps(findings, indent=2)[:8000]
    return out


def scout_fuzz(args: dict) -> str:
    """Run Echidna or Medusa against a Foundry project."""
    path = args.get("path", ".")
    tool = args.get("tool", "echidna")
    contract = args.get("contract", "")
    seconds = args.get("seconds", 60)

    out = f"=== FUZZING: {tool} on {path} ===\n"

    if tool == "echidna":
        cmd = ["echidna", path, "--contract", contract, "--test-mode", "property"]
        if seconds:
            cmd += ["--test-limit", str(seconds * 1000)]
    elif tool == "medusa":
        cmd = ["medusa", "fuzz", "--target", "crytic_export"]
    else:
        return f"unknown fuzz tool: {tool}"

    out += f"$ {' '.join(cmd)}\n"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 60, cwd=path)
        out += r.stdout[-4000:] if r.stdout else ""
        if r.returncode != 0:
            out += f"\n[exit {r.returncode}]"
    except FileNotFoundError:
        out += f"\nERROR: {tool} not installed. Install with: pipx install {tool}"
    except subprocess.TimeoutExpired:
        out += f"\n[fuzzing timed out after {seconds}s]"

    return out


def scout_cast_call(args: dict) -> str:
    """Read-only cast call. Refuses state-changing methods."""
    target = args.get("target", "")
    sig = args.get("signature", "")
    args_list = args.get("args", [])
    rpc = args.get("rpc", os.environ.get("ETH_RPC", "https://eth.llamarpc.com"))

    if not target or not sig:
        return "ERROR: target and signature required"

    # Refuse state-changing methods
    state_change_keywords = ["write", "transfer", "approve", "set", "mint", "burn",
                             "withdraw", "deposit", "swap", "execute", "send",
                             "update", "modify", "change", "pause", "unpause",
                             "claim", "stake", "unstake", "lock", "unlock"]
    sig_lower = sig.lower()
    for kw in state_change_keywords:
        if kw in sig_lower:
            return f"REFUSED: '{sig}' looks state-changing. SCOUT is read-only. Confirm with operator before allowing."

    cmd = ["cast", "call", target, sig] + args_list + ["--rpc-url", rpc]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return f"OK: {r.stdout.strip()}"
        return f"ERROR: {r.stderr.strip()}"
    except FileNotFoundError:
        return "ERROR: cast not installed (install foundry)"
    except subprocess.TimeoutExpired:
        return "ERROR: call timed out"


def scout_decompile(args: dict) -> str:
    """Decompile EVM bytecode using heimdall-rs."""
    target = args.get("target", "")
    rpc = args.get("rpc", os.environ.get("ETH_RPC", "https://eth.llamarpc.com"))

    if not target.startswith("0x"):
        return "ERROR: target must be a 0x address"

    try:
        r = subprocess.run(
            ["heimdall", "decompile", "--target", target, "--rpc-url", rpc, "--output", "-"],
            capture_output=True, text=True, timeout=180,
        )
        return r.stdout[-8000:] if r.stdout else r.stderr[-1000:]
    except FileNotFoundError:
        return "ERROR: heimdall not installed. Install: curl -L https://raw.githubusercontent.com/Jon-Becker/heimdall-rs/main/install.sh | bash"
    except subprocess.TimeoutExpired:
        return "ERROR: decompile timed out"


def scout_search_findings(args: dict) -> str:
    """Search local finding store."""
    query = args.get("query", "")
    if not query:
        return "ERROR: query required"

    results = []
    # Search memory
    for mem_file in MEMORY_DIR.glob("*.json"):
        try:
            data = mem_file.read_text()
            if query.lower() in data.lower():
                results.append(f"[memory] {mem_file.name}: match")
        except Exception:
            pass
    # Search artifact dir
    for f in ARTIFACT_DIR.rglob("*.json"):
        try:
            data = f.read_text()
            if query.lower() in data.lower():
                results.append(f"[artifacts] {f}: match")
        except Exception:
            pass

    return "\n".join(results) if results else f"no findings matching '{query}'"


def scout_triage(args: dict) -> str:
    """Apply severity scoring to a raw finding."""
    finding = args.get("finding", {})
    impact_class = (finding.get("class") or finding.get("vulnerability") or "").lower()
    target = finding.get("target", "")
    reproducible = finding.get("reproducible", False)
    funds_at_risk = finding.get("funds_at_risk_usd", 0)

    # Severity rubric
    if "reentrancy" in impact_class and funds_at_risk > 1_000_000:
        severity = "Critical"
        reward_estimate = 5_000_000
    elif "oracle" in impact_class and funds_at_risk > 100_000:
        severity = "Critical"
        reward_estimate = 5_000_000
    elif "access control" in impact_class and funds_at_risk > 100_000:
        severity = "High"
        reward_estimate = 500_000
    elif funds_at_risk > 10_000:
        severity = "Medium"
        reward_estimate = 50_000
    elif funds_at_risk > 0:
        severity = "Low"
        reward_estimate = 5_000
    else:
        severity = "Informational"
        reward_estimate = 0

    exploitability = "Confirmed" if reproducible else ("Likely" if finding.get("fork_repro") else "Theoretical")

    return json.dumps({
        "severity": severity,
        "exploitability": exploitability,
        "reward_estimate_usd": reward_estimate,
        "next_step": "draft_report" if severity in ("Critical", "High") else "needs_more_work",
    }, indent=2)


def scout_draft_report(args: dict) -> str:
    """Draft a markdown bug bounty report."""
    finding = args.get("finding", {})
    title = finding.get("title", "Untitled finding")
    severity = finding.get("severity", "Medium")
    vuln_class = finding.get("class", "unknown")
    target = finding.get("target", "N/A")
    summary = finding.get("summary", "TBD")
    attack_path = finding.get("attack_path", ["TBD"])
    poc = finding.get("poc", "// TBD")
    impact = finding.get("impact", "TBD")
    fix = finding.get("fix", "TBD")

    md = f"""## [{severity}] {title}

**Vulnerability Class:** {vuln_class}
**Target:** {target}
**Status:** {finding.get("exploitability", "Likely")}

### Summary
{summary}

### Attack Path
"""
    if isinstance(attack_path, list):
        for i, step in enumerate(attack_path, 1):
            md += f"{i}. {step}\n"
    else:
        md += f"{attack_path}\n"

    md += f"""
### Proof of Concept
```
{poc}
```

### Impact
{impact}

### Recommended Fix
{fix}
"""

    # Save to artifact dir
    out_path = ARTIFACT_DIR / f"report_{int(__import__('time').time())}.md"
    out_path.write_text(md)
    return f"Report drafted, saved to {out_path}\n\n---\n{md}"


# ---------------------------------------------------------------------------
# MCP protocol wrapper (stdio JSON-RPC)
# ---------------------------------------------------------------------------

TOOLS = {
    "scout_recon": {
        "fn": scout_recon,
        "schema": {
            "name": "scout_recon",
            "description": "Run reconnaissance against a target. Returns subdomain list, asset inventory, and high-value surfaces.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Domain or contract address"},
                    "scope": {"type": "string", "enum": ["passive", "active"], "default": "passive"},
                },
                "required": ["target"],
            },
        },
    },
    "scout_scan_solidity": {
        "fn": scout_scan_solidity,
        "schema": {
            "name": "scout_scan_solidity",
            "description": "Run slither and aderyn on a Solidity codebase. Returns findings with severity and locations.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to Solidity project"},
                    "detectors": {"type": "string", "default": "medium,high,critical"},
                },
                "required": ["path"],
            },
        },
    },
    "scout_fuzz": {
        "fn": scout_fuzz,
        "schema": {
            "name": "scout_fuzz",
            "description": "Run Echidna or Medusa against a Foundry project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "tool": {"type": "string", "enum": ["echidna", "medusa"], "default": "echidna"},
                    "contract": {"type": "string"},
                    "seconds": {"type": "integer", "default": 60},
                },
                "required": ["path", "contract"],
            },
        },
    },
    "scout_cast_call": {
        "fn": scout_cast_call,
        "schema": {
            "name": "scout_cast_call",
            "description": "Read-only cast call. Refuses state-changing methods.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "signature": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "rpc": {"type": "string"},
                },
                "required": ["target", "signature"],
            },
        },
    },
    "scout_decompile": {
        "fn": scout_decompile,
        "schema": {
            "name": "scout_decompile",
            "description": "Decompile EVM bytecode using heimdall-rs.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Contract address"},
                    "rpc": {"type": "string"},
                },
                "required": ["target"],
            },
        },
    },
    "scout_search_findings": {
        "fn": scout_search_findings,
        "schema": {
            "name": "scout_search_findings",
            "description": "Search local finding store for prior work.",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    "scout_triage": {
        "fn": scout_triage,
        "schema": {
            "name": "scout_triage",
            "description": "Apply severity scoring to a raw finding.",
            "inputSchema": {
                "type": "object",
                "properties": {"finding": {"type": "object"}},
                "required": ["finding"],
            },
        },
    },
    "scout_draft_report": {
        "fn": scout_draft_report,
        "schema": {
            "name": "scout_draft_report",
            "description": "Draft a markdown bug bounty report for a triaged finding.",
            "inputSchema": {
                "type": "object",
                "properties": {"finding": {"type": "object"}},
                "required": ["finding"],
            },
        },
    },
}


def handle_initialize(req: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "scout-security-toolkit", "version": "0.1.0"},
            "capabilities": {"tools": {"listChanged": False}},
        },
    }


def handle_list_tools(req: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req["id"],
        "result": {
            "tools": [t["schema"] for t in TOOLS.values()],
        },
    }


def handle_call_tool(req: dict) -> dict:
    name = req["params"]["name"]
    args = req["params"].get("arguments", {})
    if name not in TOOLS:
        return {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32602, "message": f"unknown tool: {name}"},
        }
    try:
        result = TOOLS[name]["fn"](args)
        return {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": {"content": [{"type": "text", "text": str(result)}]},
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": req["id"],
            "error": {"code": -32603, "message": f"tool error: {e}"},
        }


def main():
    """MCP stdio JSON-RPC loop."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        if method == "initialize":
            resp = handle_initialize(req)
        elif method == "tools/list":
            resp = handle_list_tools(req)
        elif method == "tools/call":
            resp = handle_call_tool(req)
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        else:
            resp = {
                "jsonrpc": "2.0",
                "id": req.get("id"),
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()