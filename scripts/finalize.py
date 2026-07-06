"""tools/finalize.py — N11 finalize (workflow_spec §1 N11 / §0.6.k).

The real N11 caller (gap #3: the conductor's final gate + M2 recheck existed but
nothing invoked them). Runs the finalize sequence, **hard-blocking on any failing
step**, and only on success emits review.html + the computer:// delivery line:

  1. recheck.py     — realtime retraction/EoC recheck (Crossref/PubMed, NOT the
                      N3 cache); a newly-retracted cite blocks delivery (M2).
  2. render_refs.py — re-sync References + PRISMA flow (cites may have changed).
  3. lint_review.py — final lint (exit ∈ {0,2}).
  4. write_gate.py  — final write gate (faithfulness suspect=insufficient=0 +
                      claim-map + high-risk grounding + evidence + cross-gap +
                      metadata). Run with HEALTH_REVIEW_FINALIZE=1.
  5. md_to_html.py  — review.html delivery (claim_id sidecars stripped).

    python tools/finalize.py reviews/<topic>
    # exit 0 = delivered (review.html written); non-zero = blocked at a step.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

_HERE = pathlib.Path(__file__).parent


def _topic_ref(topic_dir: pathlib.Path) -> str:
    return f"reviews/{topic_dir.name}"


def _run(cmd: list[str], extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    env = {**os.environ, "HEALTH_REVIEW_DAEMON": "0", "HEALTH_REVIEW_FINALIZE": "1"}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(cmd, cwd=str(_HERE.parent), capture_output=True, text=True, env=env)
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(tail[-8:])


def finalize(topic_dir: pathlib.Path) -> dict[str, object]:
    ref = _topic_ref(topic_dir)
    py = sys.executable
    steps_ok: list[str] = []

    def blocked(step: str, rc: int, out: str) -> dict[str, object]:
        return {"ok": False, "blocked_at": step, "exit": rc, "detail": out, "steps_ok": steps_ok}

    # 1. realtime retraction recheck (M2) — FAIL-CLOSED (spec N11/§0.6.k:「实时重核
    # 成功后才能 finalize」). recheck.py exit: 0=clean / 1=retracted/EoC/failed
    # re-verification / 2=error (couldn't reach Crossref/PubMed). Only a clean (0)
    # recheck licenses delivery: rc≥1 blocks. A transient网络 error (rc 2) must NOT
    # fail open — set HEALTH_REVIEW_ALLOW_RECHECK_FAIL=1 to accept the risk offline.
    # HEALTH_REVIEW_RECHECK_FRESH=1 forces a live Crossref/PubMed fetch (bypass the N3
    # disk cache) all the way down recheck.py → verify.py → apis.py (M2 / §0.6.k 实时重核).
    rc, out = _run([py, str(_HERE / "recheck.py"), ref], extra_env={"HEALTH_REVIEW_RECHECK_FRESH": "1"})
    if rc != 0:
        if rc >= 2 and os.environ.get("HEALTH_REVIEW_ALLOW_RECHECK_FAIL") == "1":
            steps_ok.append("recheck(skipped: ALLOW_RECHECK_FAIL — recheck could not certify)")
        else:
            return blocked("recheck", rc, out)
    else:
        steps_ok.append("recheck")

    # 2. re-render References + PRISMA
    rc, out = _run([py, str(_HERE / "render_refs.py"), f"{ref}/review.md", ref])
    if rc != 0:
        return blocked("render_refs", rc, out)
    steps_ok.append("render_refs")

    # 3. final lint
    rc, out = _run([py, str(_HERE / "lint_review.py"), ref])
    if rc not in (0, 2):
        return blocked("lint", rc, out)
    steps_ok.append("lint")

    # 4. final write gate
    # R37: write_gate passes ONLY on exit 0. Unlike lint (exit 2 = acceptable WARN), write_gate's
    # exit 2 is a bad-path hard error (not a topic dir) and 1/3 are BLOCKED — none may be treated as
    # pass. The old `rc not in (0, 2)` reused lint's WARN tolerance and silently passed a bad-path
    # write_gate run (latent: main() guards review.md exists first, but a defensive defect regardless).
    rc, out = _run([py, str(_HERE / "write_gate.py"), ref])
    if rc != 0:
        return blocked("write_gate", rc, out)
    steps_ok.append("write_gate")

    # 5. HTML delivery
    rc, out = _run([py, str(_HERE / "md_to_html.py"), f"{ref}/review.md"])
    if rc != 0:
        return blocked("md_to_html", rc, out)
    steps_ok.append("md_to_html")

    html = topic_dir / "review.html"
    return {"ok": True, "html": str(html), "delivery": f"computer://{html}", "steps_ok": steps_ok}


def main() -> None:
    parser = argparse.ArgumentParser(description="finalize — N11 收尾 (spec §1 N11).")
    parser.add_argument("topic_dir")
    args = parser.parse_args()
    topic_dir = pathlib.Path(args.topic_dir)
    if not (topic_dir / "review.md").exists():
        print(f"[ERROR] no review.md under {topic_dir}", file=sys.stderr)
        raise SystemExit(2)

    result = finalize(topic_dir)
    if result["ok"]:
        print(f"[finalize] delivered → {result['delivery']}")
        raise SystemExit(0)
    print(f"[finalize] BLOCKED at {result['blocked_at']} (exit {result['exit']}):")
    print(result["detail"])
    raise SystemExit(1)


if __name__ == "__main__":
    main()
