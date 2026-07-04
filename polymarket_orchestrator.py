#!/usr/bin/env python3
"""
SCOUT Polymarket Orchestrator — drives the 6-agent vulnerability research workflow.

Phases:
  1. Scope (operator confirms target + boundaries)
  2. Reconnaissance (asset inventory)
  3. Deep scan (web2 + web3 + RE in parallel)
  4. Triage (severity scoring + dedup)
  5. Report (draft markdown)
  6. Hand-off (operator review)

This is the real orchestrator — calls the bridge for tool execution, persists
state to disk, and emits structured logs.

Usage:
  python3 ~/scout/polymarket_orchestrator.py 0xABCD1234
  python3 ~/scout/polymarket_orchestrator.py --target https://polymarket.com
  python3 ~/scout/polymarket_orchestrator.py --resume <session-id>
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

# Reuse the bridge's tool implementations
sys.path.insert(0, str(Path.home() / "scout"))
from bridge import TOOL_DISPATCH, OLLAMA_URL, load_system_prompt  # noqa: E402

import requests  # noqa: E402

SESSION_ROOT = Path("/tmp/scout/sessions")
SESSION_ROOT.mkdir(parents=True, exist_ok=True)
MEMORY_DIR = Path.home() / "scout" / "memory"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

class Session:
    def __init__(self, session_id: str | None = None):
        self.id = session_id or f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
        self.path = SESSION_ROOT / self.id
        self.path.mkdir(parents=True, exist_ok=True)
        self.state_file = self.path / "state.json"
        self.log_file = self.path / "orchestrator.log"
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except Exception:
                pass
        return {
            "session_id": self.id,
            "started_at": time.time(),
            "phase": 0,
            "target": None,
            "scope": None,
            "findings": [],
            "errors": [],
        }

    def save(self):
        self.state["last_active"] = time.time()
        self.state_file.write_text(json.dumps(self.state, indent=2, default=str))

    def log(self, msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        with self.log_file.open("a") as f:
            f.write(line + "\n")

    def write_phase(self, phase_name: str, data: Any):
        path = self.path / f"{phase_name}.json"
        path.write_text(json.dumps(data, indent=2, default=str))
        self.log(f"wrote {path.name}")


# ---------------------------------------------------------------------------
# LLM call (Ollama, with system prompt + per-phase context)
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, model: str | None = None) -> str:
    """Single Ollama call, no tool loop. For decision-making text generation."""
    model = model or os.environ.get("SCOUT_MODEL", "scout-pm")
    r = requests.post(
        OLLAMA_URL,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]


def call_llm_with_tools(system: str, user: str, model: str | None = None, max_iters: int = 8) -> str:
    """Ollama call with tool-calling loop. For actions that need shell/file access."""
    model = model or os.environ.get("SCOUT_MODEL", "scout-pm")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    for _ in range(max_iters):
        r = requests.post(
            OLLAMA_URL,
            json={"model": model, "messages": messages, "tools": _tool_schemas(), "stream": False},
            timeout=300,
        )
        r.raise_for_status()
        msg = r.json()["message"]
        messages.append(msg)
        if not msg.get("tool_calls"):
            return msg.get("content", "")
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            args = tc["function"].get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            impl = TOOL_DISPATCH.get(name)
            try:
                result = impl(args) if impl else f"unknown tool: {name}"
            except Exception as e:
                result = f"error: {e}"
            messages.append({
                "role": "tool",
                "content": str(result)[:16000],
            })
    return "(tool loop exceeded)"


def _tool_schemas() -> list:
    """Convert bridge TOOL_DISPATCH into Ollama-format tool schemas."""
    from bridge import TOOL_SCHEMAS
    return TOOL_SCHEMAS


# ---------------------------------------------------------------------------
# Phase 1: Scope
# ---------------------------------------------------------------------------

def phase1_scope(sess: Session, target: str, scope: str | None) -> bool:
    sess.log(f"=== PHASE 1: SCOPE (target={target}) ===")
    sess.state["target"] = target
    sess.state["scope"] = scope or "Polymarket public smart contracts + web frontend; out of scope: production user data, internal admin tools"
    sess.write_phase("scope", {
        "target": target,
        "scope": sess.state["scope"],
        "operator_approved": False,  # operator must approve before phase 2
    })
    sess.log(f"scope set: {sess.state['scope']}")
    sess.log(">>> OPERATOR MUST REVIEW scope.json AND SET operator_approved=true BEFORE PHASE 2 <<<")
    return True


# ---------------------------------------------------------------------------
# Phase 2: Recon
# ---------------------------------------------------------------------------

PHASE2_SYSTEM = """You are the SCOUT reconnaissance sub-agent. Your job is to map the
attack surface of a Polymarket target. You have access to shell tools.

Steps:
1. For web2 target: enumerate subdomains (subfinder), probe HTTP endpoints (httpx), identify tech stack
2. For web3 target: identify deployed contracts, fetch bytecode, identify proxy patterns
3. Write recon results to <session_artifact_dir>/recon/ as separate JSON files
4. End with a list of 3-7 high-value surfaces to deep-scan

Be thorough but read-only. Never make state-changing calls. Use rate limits (1 req/sec).
Output a structured summary at the end."""


def phase2_recon(sess: Session) -> dict:
    sess.log("=== PHASE 2: RECONNAISSANCE ===")
    recon_dir = sess.path / "recon"
    recon_dir.mkdir(exist_ok=True)

    target = sess.state["target"]
    prompt = f"""Map the attack surface for: {target}

Write results to: {recon_dir}/

Required files:
  subdomains.txt (if web2) — one subdomain per line
  endpoints.json (if web2) — {{url, method, auth, description}}
  contracts.json (if web3) — {{address, chain, proxy_type, impl_addr, verified}}
  high_value_surfaces.json — list of 3-7 surfaces to deep-scan

When done, return a summary as JSON:
  {{"status": "success", "high_value_surfaces": [...], "files_written": [...]}}"""

    try:
        summary = call_llm_with_tools(PHASE2_SYSTEM, prompt)
        sess.log(f"recon summary:\n{summary[:500]}")
        sess.write_phase("recon_summary", {"summary": summary})
    except Exception as e:
        sess.log(f"ERROR in recon: {e}")
        sess.state["errors"].append({"phase": 2, "error": str(e)})
        return {"status": "failed", "error": str(e)}

    return {"status": "success"}


# ---------------------------------------------------------------------------
# Phase 3: Deep scan
# ---------------------------------------------------------------------------

PHASE3_WEB3_SYSTEM = """You are the SCOUT web3 scanner sub-agent. Target is a Polymarket
smart contract. Find vulnerabilities using:
  - slither <path> --filter medium,high,critical
  - aderyn <path> --json
  - manual review of high-value functions (splitPosition, mergePositions, redeem, matchOrders)

For each candidate finding, write to <artifact_dir>/web3/findings.json as:
  {id, contract, function, line, class, summary, severity_initial, reproducible, poc_path}

When done, return the final findings list as JSON. STOP if you find Critical."""


PHASE3_WEB2_SYSTEM = """You are the SCOUT web2 scanner sub-agent. Target is the Polymarket
web frontend + API. Find vulnerabilities using:
  - nuclei -u <target> -severity medium,high,critical
  - ffuf for parameter fuzzing
  - JS bundle analysis (grep for endpoints, secrets)
  - manual probes for business-logic issues

For each candidate finding, write to <artifact_dir>/web2/findings.json as:
  {id, class, target, summary, severity_initial, reproducible, poc_path, evidence_path}

When done, return the final findings list as JSON. STOP if you find Critical."""


def phase3_deep_scan(sess: Session):
    sess.log("=== PHASE 3: DEEP SCAN ===")
    target = sess.state["target"]

    # Determine web2 vs web3
    is_web3 = target.startswith("0x") and len(target) == 42
    is_web2 = target.startswith("http") or "." in target

    if is_web3:
        sess.log("dispatching web3 scanner...")
        try:
            result = call_llm_with_tools(
                PHASE3_WEB3_SYSTEM,
                f"Target contract: {target}\nArtifact dir: {sess.path}/web3/\nClone the Polymarket contracts repo first if needed, then scan.",
            )
            sess.write_phase("web3_scan_result", {"output": result})
        except Exception as e:
            sess.log(f"ERROR in web3 scan: {e}")
            sess.state["errors"].append({"phase": 3, "scope": "web3", "error": str(e)})

    if is_web2:
        sess.log("dispatching web2 scanner...")
        try:
            result = call_llm_with_tools(
                PHASE3_WEB2_SYSTEM,
                f"Target URL: {target}\nArtifact dir: {sess.path}/web2/",
            )
            sess.write_phase("web2_scan_result", {"output": result})
        except Exception as e:
            sess.log(f"ERROR in web2 scan: {e}")
            sess.state["errors"].append({"phase": 3, "scope": "web2", "error": str(e)})


# ---------------------------------------------------------------------------
# Phase 4: Triage
# ---------------------------------------------------------------------------

PHASE4_SYSTEM = """You are the SCOUT triage sub-agent. Take all raw findings from
web2/, web3/, and re/ subdirectories, deduplicate, score severity using Polymarket's
tier scale, check for known issues, and output the triaged list as JSON.

Polymarket severity tiers:
  Critical ($5M): severe financial loss or system disruption
  High ($500K): significant financial harm or trust damage
  Medium ($50K): limited financial impact with operational disruption
  Low ($5K): minor deviation from intended behavior

Note: Critical requires BOTH severity AND realistic exploitability. Theoretical
issues with Critical impact but no PoC are NOT Critical.

Output:
  {id, raw_source, title, severity, reward_estimate_usd, exploitability,
   blast_radius, remediation_difficulty, known, ready_for_report}"""


def phase4_triage(sess: Session) -> dict:
    sess.log("=== PHASE 4: TRIAGE ===")
    try:
        result = call_llm(
            PHASE4_SYSTEM,
            f"""Triage all findings for session {sess.id}.

Read raw findings from:
  {sess.path}/web2/findings.json
  {sess.path}/web3/findings.json
  {sess.path}/re/findings.json

Output the triaged list as JSON. Save to {sess.path}/triage/triaged.json""",
        )
        sess.write_phase("triage_result", {"output": result})
        return {"status": "success"}
    except Exception as e:
        sess.log(f"ERROR in triage: {e}")
        sess.state["errors"].append({"phase": 4, "error": str(e)})
        return {"status": "failed", "error": str(e)}


# ---------------------------------------------------------------------------
# Phase 5: Report
# ---------------------------------------------------------------------------

PHASE5_SYSTEM = """You are the SCOUT reporter sub-agent. Take the triaged findings
list and produce a markdown report following Polymarket's expected format.

For each finding, output a section with:
  ## [SEVERITY] Title
  **Vulnerability Class:** CWE-xxx
  **Target:** specific contract/endpoint
  **Status:** Confirmed/Likely/Theoretical
  **Estimated Impact:** $X + description
  ### Summary
  ### Vulnerability Details
  ### Steps to Reproduce (concrete commands)
  ### Proof of Concept (code block)
  ### Impact Analysis
  ### Remediation

Write the report to <artifact_dir>/report.md and return a one-line summary per finding."""


def phase5_report(sess: Session):
    sess.log("=== PHASE 5: REPORT ===")
    try:
        result = call_llm(
            PHASE5_SYSTEM,
            f"""Read triaged findings from {sess.path}/triage/triaged.json
and produce the final report at {sess.path}/report.md.

Return a one-line summary per finding in JSON format:
  {{"findings": [{{"id": "...", "severity": "...", "one_liner": "..."}}], "ready_for_submission": [...]}}""",
        )
        sess.write_phase("report_result", {"output": result})
    except Exception as e:
        sess.log(f"ERROR in report: {e}")
        sess.state["errors"].append({"phase": 5, "error": str(e)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="contract address or URL to audit")
    ap.add_argument("--target", help="explicit target")
    ap.add_argument("--scope", help="scope string")
    ap.add_argument("--resume", help="resume from session-id")
    ap.add_argument("--phase", type=int, default=1, help="start from phase N (1-5)")
    args = ap.parse_args()

    target = args.target or args.target
    if not args.resume and not target:
        ap.error("target required (or use --resume <session-id>)")

    sess = Session(args.resume)
    sess.log(f"SCOUT Polymarket Orchestrator — session {sess.id}")
    sess.log(f"artifact dir: {sess.path}")

    if not args.resume:
        # Phase 1
        phase1_scope(sess, target, args.scope)

    # Phase 2
    if args.phase <= 2:
        phase2_recon(sess)

    # Phase 3
    if args.phase <= 3:
        phase3_deep_scan(sess)

    # Phase 4
    if args.phase <= 4:
        phase4_triage(sess)

    # Phase 5
    if args.phase <= 5:
        phase5_report(sess)

    sess.save()
    sess.log(f"=== DONE. Session: {sess.id} ===")
    sess.log(f"Artifacts: {sess.path}/")
    sess.log(f"Report (if generated): {sess.path}/report.md")


if __name__ == "__main__":
    main()