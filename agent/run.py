"""CLI entrypoint.

    python -m agent.run                    # all 8 cases, one shared run/clock
    python -m agent.run --case case-03     # a single case
    python -m agent.run --run-id my-run    # choose the run id (isolation)
    python -m agent.run --reset            # reset the run first (fresh clock)

The same agent implementation handles every case — no branching on case_id.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

# Use the OS trust store (macOS keychain) instead of certifi so TLS-inspection
# proxies with a corporate root CA are trusted — same certs curl already uses.
# No-op / harmless on machines that don't need it or don't have truststore.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

load_dotenv()  # pick up .env before importing the client

# llm.py lives at the repo root; make it importable regardless of CWD.
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))
from llm import get_llm_client  # noqa: E402

from .agent import CaseAgent  # noqa: E402
from .world_client import WorldClient  # noqa: E402


async def _run(run_id: str, only_case: str | None, reset: bool, verbose: bool) -> None:
    client, model = get_llm_client()
    print(f"Using model/deployment: {model}", flush=True)

    world = WorldClient(run_id=run_id)
    try:
        if reset:
            await world._request("POST", f"/runs/{run_id}/reset")
            print(f"Run '{run_id}' reset.", flush=True)

        cases = await world.list_cases()
        if not cases["ok"]:
            print(f"Could not list cases: {cases['error']}", file=sys.stderr)
            return
        case_ids = [c["case_id"] for c in cases["data"]]
        if only_case:
            if only_case not in case_ids:
                print(f"No such case '{only_case}'. Available: {case_ids}", file=sys.stderr)
                return
            case_ids = [only_case]

        results = []
        for cid in case_ids:
            print(f"\n===== {cid} =====", flush=True)
            agent = CaseAgent(client, model, world, cid, run_id, verbose=verbose)
            results.append(await agent.run())

        print("\n===== SUMMARY =====", flush=True)
        for r in results:
            print(f"{r['case_id']}: {r['terminal_state']:>10}  "
                  f"(steps={r['steps']}, evidence={r['evidence_count']})", flush=True)
        print(f"\nFetch the full action log with:  "
              f"curl -s -H 'X-Run-Id: {run_id}' http://localhost:8000/run/report", flush=True)
    finally:
        await world.aclose()


def main() -> None:
    p = argparse.ArgumentParser(description="DME Care Advocate agent")
    p.add_argument("--run-id", default="agent-run", help="X-Run-Id to use (isolated run).")
    p.add_argument("--case", default=None, help="Work only this case id.")
    p.add_argument("--reset", action="store_true", help="Reset the run (fresh clock) first.")
    p.add_argument("--quiet", action="store_true", help="Suppress per-step trace.")
    args = p.parse_args()
    asyncio.run(_run(args.run_id, args.case, args.reset, verbose=not args.quiet))


if __name__ == "__main__":
    main()
