"""
SCOUT — Sub-Agent Base Classes
Each specialist inherits from LocalAgent and implements execute().
"""

import uuid
import json
import os
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, List, Any


class LocalAgent(ABC):
    def __init__(self, name: str, domain: str, objective: str):
        self.agent_id = str(uuid.uuid4())[:8]
        self.name = name
        self.domain = domain
        self.objective = objective
        self._session_id: str = ""

    def bind_session(self, session_id: str):
        self._session_id = session_id

    def log(self, action: str, level: str = "INFO"):
        import datetime
        ts = datetime.datetime.now().isoformat()
        line = f"[{ts}] [{level}] [{self.name}:{self.agent_id}] {action}"
        print(line)
        if self._session_id:
            log_dir = f"/tmp/scout/sessions/{self._session_id}/logs"
            os.makedirs(log_dir, exist_ok=True)
            with open(f"{log_dir}/{self.name}.log", "a") as f:
                f.write(line + "\n")

    def run_cmd(self, cmd: List[str], timeout: int = 120) -> str:
        self.log(f"RUN: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            self.log(f"TIMEOUT: {' '.join(cmd)}", "WARN")
            return ""
        except FileNotFoundError:
            self.log(f"TOOL NOT FOUND: {cmd[0]}", "WARN")
            return f"[TOOL NOT INSTALLED: {cmd[0]}]"

    @abstractmethod
    def execute(self, context: Dict) -> Dict:
        raise NotImplementedError


# ── Specialist implementations ───────────────────────────────────────────────

class ReconAgent(LocalAgent):
    def __init__(self):
        super().__init__("recon-agent", "Web2/Web3", "Attack surface mapping")

    def execute(self, context: Dict) -> Dict:
        targets = context.get("scope", {}).get("targets", [])
        findings: Dict[str, Any] = {"endpoints": [], "subdomains": [], "tech": []}

        for target in targets:
            if target.startswith("http"):
                self.log(f"Subdomain enum: {target}")
                # subfinder -d <domain> -silent
                # httpx -status-code -title
                findings["subdomains"].append(f"[PLACEHOLDER: run subfinder on {target}]")
            elif target.startswith("0x"):
                self.log(f"Contract recon: {target}")
                # cast storage <addr> <erc1967_slot>
                findings["endpoints"].append(f"[PLACEHOLDER: resolve proxy impl for {target}]")

        return {"status": "ok", "agent": self.name, "data": findings}


class Web2Scanner(LocalAgent):
    def __init__(self):
        super().__init__("web2-scanner", "HTTP/API", "Web2 vulnerability scanning")

    def execute(self, context: Dict) -> Dict:
        self.log("Running nuclei template scan")
        # nuclei -u <target> -t nuclei-templates/
        self.log("Running parameter discovery (arjun)")
        # arjun -u <target>
        return {
            "status": "ok",
            "agent": self.name,
            "findings": [],  # populated by real tool output
        }


class Web3Scanner(LocalAgent):
    def __init__(self):
        super().__init__("web3-scanner", "EVM", "Smart contract static analysis")

    def execute(self, context: Dict) -> Dict:
        target_dir = context.get("source_dir", ".")
        self.log(f"Running slither on {target_dir}")
        output = self.run_cmd(["slither", target_dir, "--json", "-"])
        # parse slither JSON output into findings
        try:
            slither_data = json.loads(output) if output and output.startswith("{") else {}
        except json.JSONDecodeError:
            slither_data = {}

        return {
            "status": "ok",
            "agent": self.name,
            "slither_raw": slither_data,
            "findings": [],
        }


class ReverseEngineer(LocalAgent):
    def __init__(self):
        super().__init__("reverse-engineer", "Binary/EVM", "Bytecode and binary RE")

    def execute(self, context: Dict) -> Dict:
        addr = context.get("address", "")
        rpc = context.get("rpc", "")
        self.log(f"Decompiling {addr}")
        # heimdall decompile <addr> --rpc <rpc>
        return {"status": "ok", "agent": self.name, "decompiled": "[PLACEHOLDER]"}


class Triager(LocalAgent):
    def __init__(self):
        super().__init__("triager", "All", "Severity scoring and deduplication")

    def execute(self, context: Dict) -> Dict:
        raw_findings: List[Dict] = context.get("raw_findings", [])
        triaged = []
        for f in raw_findings:
            if not f.get("reproducible", False):
                self.log(f"DISCARDED (not reproducible): {f.get('title', '?')}", "WARN")
                continue
            severity = f.get("severity", "Medium")
            triaged.append({**f, "status": "triaged", "severity": severity})
        self.log(f"Triage complete: {len(triaged)}/{len(raw_findings)} passed")
        return {"status": "ok", "agent": self.name, "triaged": triaged}


class Reporter(LocalAgent):
    def __init__(self):
        super().__init__("reporter", "All", "Final report drafting")

    def execute(self, context: Dict) -> Dict:
        findings: List[Dict] = context.get("triaged_findings", [])
        session_id = context.get("session_id", "unknown")
        report_path = f"/tmp/scout/sessions/{session_id}/report.md"
        os.makedirs(os.path.dirname(report_path), exist_ok=True)

        lines = [f"# SCOUT Audit Report\n**Session:** {session_id}\n\n---\n"]
        for f in findings:
            lines.append(self._render_finding(f))
        report_md = "\n".join(lines)

        with open(report_path, "w") as fp:
            fp.write(report_md)
        self.log(f"Report saved: {report_path}")
        return {"status": "ok", "agent": self.name, "report_path": report_path}

    def _render_finding(self, f: Dict) -> str:
        return f"""## [{f.get('severity', 'Unknown')}] {f.get('title', 'Untitled')}

**Target:** {f.get('target', 'N/A')}
**Class:** {f.get('vuln_class', 'N/A')}
**Status:** {f.get('confidence', 'Theoretical')}

### Summary
{f.get('summary', '_No summary provided._')}

### Attack Path
{f.get('attack_path', '1. TODO')}

### Proof of Concept
```
{f.get('poc', '# TODO: add minimal reproducible PoC')}
```

### Impact
{f.get('impact', 'TODO')}

### Recommended Fix
{f.get('fix', 'TODO')}

---
"""
