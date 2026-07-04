"""
SCOUT Executor — Phase orchestrator for the full 6-phase workflow.
Run: python scout_executor.py
"""

import json
import os
import sys
import uuid
from typing import Dict, List

from agent_base import (
    ReconAgent, Web2Scanner, Web3Scanner,
    ReverseEngineer, Triager, Reporter,
)


class ScoutExecutor:
    MAX_CONCURRENT_AGENTS = 4

    def __init__(self):
        self.session_id = f"scout-{uuid.uuid4().hex[:8]}"
        self.base_path = f"/tmp/scout/sessions/{self.session_id}"
        os.makedirs(f"{self.base_path}/recon", exist_ok=True)
        os.makedirs(f"{self.base_path}/logs", exist_ok=True)
        self.scope: Dict = {}
        self.raw_findings: List[Dict] = []
        self.triaged_findings: List[Dict] = []
        print(f"scout online. session {self.session_id}")

    # ── Phase 1: Scope ───────────────────────────────────────────────────────

    def phase1_scope(
        self,
        targets: List[str],
        exclusions: str = "",
        engagement: str = "research_only",
        output_format: str = "markdown",
    ):
        """MANDATORY before any scanning. Confirm with operator before calling."""
        self.scope = {
            "targets": targets,
            "exclusions": exclusions,
            "engagement": engagement,
            "output_format": output_format,
        }
        self._save("scope.json", self.scope)
        print(f"[Phase 1] Scope confirmed. Targets: {targets}")

    # ── Phase 2: Recon ───────────────────────────────────────────────────────

    def phase2_recon(self) -> Dict:
        assert self.scope, "[ABORT] phase1_scope() must be called first"
        print("[Phase 2] Starting reconnaissance...")
        agent = ReconAgent()
        agent.bind_session(self.session_id)
        result = agent.execute({"scope": self.scope, "session_id": self.session_id})
        self._save("recon/recon.json", result)
        print(f"[Phase 2] Recon complete → {self.base_path}/recon/recon.json")
        return result

    # ── Phase 3: Deep scan ───────────────────────────────────────────────────

    def phase3_deep_scan(self, high_value_surfaces: List[str]) -> List[Dict]:
        """Spawn one agent per surface. Respect MAX_CONCURRENT_AGENTS."""
        assert self.scope, "[ABORT] phase1_scope() must be called first"
        print(f"[Phase 3] Deep scan: {len(high_value_surfaces)} surfaces")
        all_results = []

        for i, surface in enumerate(high_value_surfaces[:self.MAX_CONCURRENT_AGENTS]):
            print(f"  [{i+1}] {surface}")
            agent = self._pick_agent(surface)
            agent.bind_session(self.session_id)
            ctx = {
                "scope": self.scope,
                "session_id": self.session_id,
                "surface": surface,
                "recon": self._load("recon/recon.json"),
                "source_dir": surface if os.path.isdir(surface) else ".",
                "address": surface if surface.startswith("0x") else "",
            }
            result = agent.execute(ctx)
            all_results.append(result)
            self._save(f"scan_{i}.json", result)

        return all_results

    def _pick_agent(self, surface: str):
        if surface.startswith("0x") or "contract" in surface.lower():
            return Web3Scanner()
        if os.path.isdir(surface):
            return Web3Scanner()
        return Web2Scanner()

    # ── Phase 4: Triage ──────────────────────────────────────────────────────

    def phase4_triage(self) -> List[Dict]:
        print("[Phase 4] Triage...")
        agent = Triager()
        agent.bind_session(self.session_id)
        result = agent.execute({
            "raw_findings": self.raw_findings,
            "session_id": self.session_id,
        })
        self.triaged_findings = result.get("triaged", [])
        self._save("triaged.json", self.triaged_findings)
        print(f"[Phase 4] {len(self.triaged_findings)} findings passed triage")
        return self.triaged_findings

    # ── Phase 5–6: Report & hand-off ─────────────────────────────────────────

    def phase5_report(self) -> str:
        print("[Phase 5] Generating report...")
        agent = Reporter()
        agent.bind_session(self.session_id)
        result = agent.execute({
            "triaged_findings": self.triaged_findings,
            "session_id": self.session_id,
        })
        report_path = result.get("report_path", "")
        print(f"[Phase 6] Report → {report_path}")
        print("operator: submit, deepen, or move on?")
        return report_path

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _save(self, key: str, data):
        path = os.path.join(self.base_path, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def _load(self, key: str) -> Dict:
        path = os.path.join(self.base_path, key)
        try:
            with open(path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def add_finding(self, finding: Dict):
        """Manually add a finding (e.g., from manual review) to the queue."""
        self.raw_findings.append(finding)

    @classmethod
    def from_scope_file(cls, scope_json: str) -> "ScoutExecutor":
        """Resume a session from a saved scope file."""
        ex = cls()
        with open(scope_json) as f:
            ex.scope = json.load(f)
        return ex


# ── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SCOUT Executor")
    parser.add_argument("targets", nargs="+", help="Domains or 0x contract addresses")
    parser.add_argument("--exclusions", default="", help="Out-of-scope items")
    parser.add_argument("--engagement", default="research_only",
                        choices=["responsible_disclosure", "audit", "research_only"])
    parser.add_argument("--auto", action="store_true",
                        help="Skip scope confirmation prompt")
    args = parser.parse_args()

    scout = ScoutExecutor()

    if not args.auto:
        print(f"\nScope to confirm:")
        print(f"  targets    : {args.targets}")
        print(f"  exclusions : {args.exclusions or 'none'}")
        print(f"  engagement : {args.engagement}")
        go = input("\nProceed? [y/N] ").strip().lower()
        if go != "y":
            print("Aborted.")
            sys.exit(0)

    scout.phase1_scope(args.targets, args.exclusions, args.engagement)
    scout.phase2_recon()
    scout.phase3_deep_scan(args.targets)
    scout.phase4_triage()
    scout.phase5_report()
