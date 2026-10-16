"""Run Codex SlopCodeBench objectives and Prune objective-by-objective.

Codex is invoked by SlopCodeBench in its Docker runtime.  The benchmark's
evaluation tests and gold snapshots are outside the agent workspace; this
driver never mounts them into the Codex container.  After Codex completes a
problem, each adjacent checkpoint pair is analyzed by Prune and the Docker
container/image cleanup hook runs before the next objective is started.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SLOP = ROOT / "datasets/slop-code-bench"
PYTHON = ROOT / ".venv/bin/python"
PRUNE = Path(__file__).with_name("codeslop_smoke.py")
DEFAULT_RUN = SLOP / "outputs/codex_luna_efficient/20260916T193336"
DEFAULT_IMAGE = "ghcr.io/astral-sh/uv:python3.12-trixie-slim"
DEFAULT_CONFIG = ROOT / "model/prune-version/config/default.yaml"


def _load_api_key(config: Path) -> None:
    """Populate the configured API-key environment variable without logging it."""
    env_name = "OPENAI_API_KEY"
    configured_key = ""
    if config.exists():
        try:
            import yaml
            values = yaml.safe_load(config.read_text()) or {}
            env_name = str(values.get("openai_api_key_env", env_name))
            configured_key = str(values.get("openai_api_key", "") or "")
        except Exception:
            # Keep the environment-only path usable if optional YAML parsing is absent.
            pass
    if not os.environ.get(env_name) and configured_key:
        os.environ[env_name] = configured_key


def _problems(run: Path, requested: list[str]) -> list[str]:
    if requested:
        return requested
    return sorted(p.name for p in run.iterdir() if p.is_dir() and
                  (p / "checkpoint_1").is_dir())


def _checkpoint_numbers(problem_dir: Path) -> list[int]:
    out = []
    for p in problem_dir.glob("checkpoint_*"):
        try:
            if (p / "snapshot").is_dir():
                out.append(int(p.name.split("_", 1)[1]))
        except ValueError:
            pass
    return sorted(out)


def _codex_stats(path: Path) -> dict:
    evaluation = path / "evaluation.json"
    if not evaluation.exists():
        return {"available": False}
    try:
        value = json.loads(evaluation.read_text())
        return {"available": True, "evaluation": value}
    except json.JSONDecodeError as exc:
        return {"available": False, "error": str(exc)}


def _cleanup(image: str) -> dict:
    """Remove only containers/images owned by this SlopCode environment."""
    commands = [
        ["docker", "ps", "-aq", "--filter", f"ancestor={image}"],
    ]
    removed_containers: list[str] = []
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True)
        if result.returncode == 0:
            removed_containers = [x for x in result.stdout.split() if x]
            if removed_containers:
                subprocess.run(["docker", "rm", "-f", *removed_containers],
                               check=False, capture_output=True)
    image_result = subprocess.run(["docker", "image", "rm", "-f", image],
                                  text=True, capture_output=True)
    return {"image": image, "removed_containers": removed_containers,
            "image_removed": image_result.returncode == 0,
            "image_error": image_result.stderr[-1000:] if image_result.returncode else ""}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN,
                        help="Completed SlopCode Codex run directory.")
    parser.add_argument("--problem", action="append", default=[],
                        help="Problem to process; repeat for multiple problems.")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "smoke-results/slopcode-prune")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                        help="YAML config containing openai_api_key or its environment name.")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--run-codex", action="store_true",
                        help="Run SlopCodeBench Codex first, then use its newest output run.")
    args = parser.parse_args()
    _load_api_key(args.config)

    if args.run_codex:
        hook = ROOT / "model/prune-version/slopcode_prune_hook.py"
        args.output.mkdir(parents=True, exist_ok=True)
        os.environ["SLOPCODE_PRUNE_OUTPUT"] = str(args.output.resolve())
        os.environ["SLOPCODE_POST_CHECKPOINT_HOOK"] = (
            f"{PYTHON} {hook} --model {args.model}"
        )
        command = [str(SLOP / ".venv/bin/slop-code"), "run",
                   "--config", str(SLOP / "configs/runs/codex_luna_efficient.yaml"),
                   "--num-workers", "1", "--no-concurrent-evaluation",
                   "--live-progress"]
        for problem in args.problem:
            command += ["--problem", problem]
        subprocess.run(command, cwd=SLOP, check=True)
        candidates = sorted((SLOP / "outputs/codex_luna_efficient").iterdir(),
                            key=lambda p: p.stat().st_mtime)
        args.run = candidates[-1]
        print(json.dumps({"stage": "interleaved_complete", "run": str(args.run),
                          "prune_output": str(args.output)}), flush=True)
        return 0

    problems = _problems(args.run, args.problem)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "pipeline.json"
    state = {"run": str(args.run), "model": args.model, "problems": {}}
    if args.resume and manifest.exists():
        state = json.loads(manifest.read_text())

    for problem in problems:
        source = args.run / problem
        checkpoints = _checkpoint_numbers(source)
        problem_state = state["problems"].setdefault(problem, {"objectives": []})
        done = {(x["from_checkpoint"], x["to_checkpoint"])
                for x in problem_state["objectives"]}
        for old, new in zip(checkpoints, checkpoints[1:]):
            if (old, new) in done:
                continue
            artifact = args.output / problem / f"checkpoint_{new}"
            artifact.mkdir(parents=True, exist_ok=True)
            objective = {
                "from_checkpoint": old,
                "to_checkpoint": new,
                "codex": _codex_stats(source / f"checkpoint_{new}"),
                "stage": "prune_running",
                "started_at": time.time(),
            }
            (artifact / "codex_stats.json").write_text(
                json.dumps(objective["codex"], indent=2))
            command = [str(PYTHON), str(PRUNE), "--run", str(args.run),
                       "--problem", problem, "--from-checkpoint", str(old),
                       "--to-checkpoint", str(new), "--model", args.model,
                       "--output", str(artifact)]
            run = subprocess.run(command, cwd=ROOT, text=True,
                                 capture_output=True)
            (artifact / "prune.stdout.log").write_text(run.stdout)
            (artifact / "prune.stderr.log").write_text(run.stderr)
            objective.update({"stage": "complete" if run.returncode == 0 else "error",
                              "returncode": run.returncode,
                              "finished_at": time.time(),
                              "cleanup": _cleanup(args.image)})
            result = artifact / "result.json"
            if result.exists():
                objective["prune_result"] = str(result)
            problem_state["objectives"].append(objective)
            manifest.write_text(json.dumps(state, indent=2))
            print(json.dumps({"problem": problem, "objective": f"{old}->{new}",
                              "stage": objective["stage"],
                              "returncode": run.returncode}), flush=True)

    manifest.write_text(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
