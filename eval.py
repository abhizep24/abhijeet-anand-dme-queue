"""Offline evaluator — read only, never touched by the agent at runtime.

Compares the final GET /run/report against the expected terminal state for each
case, which is derived offline from data/world.yaml. `data/world.yaml` is
explicitly readable for evaluation; it is NEVER consulted by the agent.

    python eval.py --run-id agent-run

Expected terminal states are derived from the world's own mechanics: a case is
"resolvable" if some reachable sequence of facts leads to a terminal-good inbox/
claim state; otherwise the correct answer is "escalated". Rather than re-simulate
the world here, we encode the expected outcome per case as read off world.yaml,
with the yaml evidence noted so the judgment is auditable. This keeps the eval
honest without giving the agent any signal.
"""

from __future__ import annotations

import argparse
import sys

import httpx
import yaml

# Expected terminal state per case, with the world.yaml evidence for each call.
# Derived by reading data/world.yaml only. Not used by the agent.
EXPECTED = {
    "case-01": ("resolved",  "Docs faxed to correct line 0142 -> supplier_has_documentation -> resubmit lands PAID."),
    "case-02": ("resolved",  "Text yields member id -> Humana confirms new payer -> supplier rebills -> claim ACCEPTED."),
    "case-03": ("resolved",  "Fax PT eval to 0178 -> aetna_has_clinicals -> PA APPROVED after 5 days."),
    "case-04": ("resolved",  "Prairie dead/full; Windy City confirms -> fax order -> delivery scheduled -> delivered."),
    "case-05": ("escalated", "Patient hospitalized indefinitely; supplier hold lapses; no delivery window obtainable."),
    "case-06": ("resolved",  "Call Northside (working fax 0289) -> corrected K0001 order -> supplier can bill."),
    "case-07": ("escalated", "Referral expires day+28; PCP redirect/extension takes ~2wk, uncertain; no in-window path."),
    "case-08": ("resolved",  "PT eval supports E0143 -> Cicero corrects order -> exchange scheduled -> complete."),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--run-id", default="agent-run")
    p.add_argument("--base-url", default="http://localhost:8000")
    args = p.parse_args()

    resp = httpx.get(f"{args.base_url}/run/report", headers={"X-Run-Id": args.run_id}, timeout=30)
    resp.raise_for_status()
    report = resp.json()
    actual = {c["case_id"]: c["status"] for c in report["cases"]}

    print(f"Run: {report['run_id']}  (day {report['day']}, {report['total_actions']} actions)\n")
    header = f"{'case':<9} {'expected':<11} {'actual':<11} {'result'}"
    print(header)
    print("-" * len(header))
    correct = 0
    scored = 0
    for cid, (exp, why) in EXPECTED.items():
        act = actual.get(cid, "open")
        if act == "open":
            mark = "— not attempted"
        else:
            scored += 1
            ok = act == exp
            correct += ok
            mark = "CORRECT" if ok else "WRONG"
        print(f"{cid:<9} {exp:<11} {act:<11} {mark}")
        if act != "open" and act != exp:
            print(f"          ↳ expected because: {why}")

    print(f"\nTerminal accuracy: {correct}/{scored} attempted correct "
          f"({correct}/{len(EXPECTED)} of all cases).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
