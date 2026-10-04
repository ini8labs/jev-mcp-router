"""Jev latency without load: requests one at a time (run.py's numbers include queueing at 16 concurrent)."""
import asyncio, statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent)); sys.path.insert(0, str(Path(__file__).parent.parent))
import run
from jevmcp.jev import Jev

async def main(n=10):
    cat, reqs, jev = run.load_catalog(), run.load_requests(), Jev()
    servers = sorted({t["server"] for t in cat})
    flat, two = [], []
    for req in reqs[:: len(reqs) // n][:n]:
        state = {"user_request": req["request"], "context": run.CONTEXT}
        t0 = time.perf_counter()
        await jev.decide(state, {t["id"]: run.tool_q(t) for t in cat})
        flat.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        s = await jev.decide(state, {s: run.server_q(s) for s in servers})
        keep = {k for k, p in s.p_yes.items() if p >= 0.3}
        await jev.decide(state, {t["id"]: run.tool_q(t) for t in cat if t["server"] in keep})
        two.append(time.perf_counter() - t0)
    print(f"flat (649 questions, 17 parallel requests): p50 {statistics.median(flat):.2f}s  max {max(flat):.2f}s")
    print(f"2-step (25 server questions, then tools of YES servers): p50 {statistics.median(two):.2f}s  max {max(two):.2f}s")

asyncio.run(main())
