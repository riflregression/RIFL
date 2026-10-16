"""Post-checkpoint hook for true Codex -> Prune -> next-objective ordering."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv/bin/python"
PRUNE = Path(__file__).with_name("codeslop_smoke.py")
IMAGE = "ghcr.io/astral-sh/uv:python3.12-trixie-slim"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--problem", required=True)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--model", default="gpt-5.6-luna")
    args = ap.parse_args()
    target = int(args.checkpoint.split("_", 1)[1])
    # checkpoint_0 is captured by AgentRunner before objective 1 starts.
    # Its own checkpoint tests are the only available preservation suite for
    # objective 1; later objectives use the immediately preceding checkpoint.
    old = target - 1
    p2p_checkpoint = old if old > 0 else 1
    output_root = Path(os.environ.get("SLOPCODE_PRUNE_OUTPUT", str(args.run_dir)))
    output_root = output_root.resolve()
    artifact = output_root / args.problem / args.checkpoint
    artifact.mkdir(parents=True, exist_ok=True)
    if target <= 1:
        # There is no prior objective-level regression suite for checkpoint 1.
        # Record this explicitly instead of treating checkpoint-1 tests as P2P.
        (artifact / "result.json").write_text(json.dumps({
            "problem": args.problem, "from_checkpoint": 0,
            "to_checkpoint": target, "status": "P2P_UNAVAILABLE",
            "reason": "No prior objective checkpoint/test suite exists",
            "evaluations": [],
        }, indent=2))
        print(json.dumps({"final_snapshot": None,
                          "prune_returncode": 0,
                          "status": "P2P_UNAVAILABLE",
                          "artifact": str(artifact)}))
        return 0
    run_dir = args.run_dir.resolve()
    command = [str(PYTHON), str(PRUNE), "--run", str(run_dir),
               "--problem", args.problem, "--from-checkpoint", str(old),
               "--to-checkpoint", str(target), "--p2p-checkpoint",
               str(p2p_checkpoint), "--model", args.model,
               "--docker-image", IMAGE,
               "--output", str(artifact)]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    (artifact / "hook-prune.stdout.log").write_text(result.stdout)
    (artifact / "hook-prune.stderr.log").write_text(result.stderr)
    final_snapshot = None
    result_json = artifact / "result.json"
    if result.returncode == 0 and result_json.exists():
        data = json.loads(result_json.read_text())
        for evaluation in data.get("evaluations", []):
            if evaluation.get("status") == "PASS" and evaluation.get("final_snapshot"):
                final_snapshot = evaluation["final_snapshot"]
                break
    # Explicitly remove containers and the base image before the next objective.
    ids = subprocess.run(["docker", "ps", "-aq", "--filter", f"ancestor={IMAGE}"],
                         text=True, capture_output=True)
    container_ids = [x for x in ids.stdout.split() if x]
    if container_ids:
        subprocess.run(["docker", "rm", "-f", *container_ids], check=False,
                       capture_output=True)
    subprocess.run(["docker", "image", "rm", "-f", IMAGE], check=False,
                   capture_output=True)
    print(json.dumps({"final_snapshot": final_snapshot,
                      "prune_returncode": result.returncode,
                      "artifact": str(artifact)}))
    return 0 if result.returncode == 0 else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
