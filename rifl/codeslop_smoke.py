"""Run the prune-version subset analysis on one CodeSlopBench checkpoint.

This is intentionally small and dataset-specific: CodeSlopBench stores each
checkpoint as a snapshot rather than a Docker/FeatBench task.  The script
uses checkpoint 1 as R_old and checkpoint 2 as the feature patch, then runs
checkpoint-2 feature tests (F2P) and the complete checkpoint-1 suite (P2P)
for every proposed subset. It is useful as a reproducible motivating example for regression
causing a later feature failure.
"""
from __future__ import annotations

import argparse
import ast
import difflib
from itertools import combinations
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / "datasets/slop-code-bench/outputs/codex_luna_efficient/20260916T193336"
SCBENCH = ROOT / "datasets/slop-code-bench/.scbench/problems"
SCBENCH_PYTHON = ROOT / "datasets/slop-code-bench/.venv/bin/python"


def _source_files(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}


def _sections(diff: str) -> list[str]:
    lines = diff.splitlines()
    starts = [i for i, x in enumerate(lines) if x.startswith("@@")]
    return ["\n".join(lines[i:(starts[n + 1] if n + 1 < len(starts) else len(lines))]) + "\n"
            for n, i in enumerate(starts)]


def _symbol(source: str, line: int) -> str:
    try:
        tree = ast.parse(source)
        nodes = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 and n.lineno <= line <= getattr(n, "end_lineno", n.lineno)]
        return max(nodes, key=lambda n: n.lineno).name if nodes else "<file-scope>"
    except (SyntaxError, ValueError):
        return "<file-scope>"


def _is_import_only(section: str) -> bool:
    changed = [x[1:].strip() for x in section.splitlines()[1:]
               if x.startswith(("+", "-")) and not x.startswith(("+++", "---")) and x[1:].strip()]
    return bool(changed) and all(re.match(r"(?:from\s+\S+\s+import|import)\b", x) for x in changed)


def extract_hunks(old: dict[str, str], new: dict[str, str]) -> list[dict]:
    grouped: dict[tuple[str, str], dict] = {}
    for filename in sorted(set(old) | set(new)):
        before, after = old.get(filename, ""), new.get(filename, "")
        diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                             fromfile=f"a/{filename}", tofile=f"b/{filename}", n=3))
        if not diff:
            continue
        for section in _sections(diff):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", section)
            old_line, new_line = (int(m.group(1)), int(m.group(2))) if m else (1, 1)
            symbol = _symbol(before, old_line) if before else _symbol(after, new_line)
            key = (filename, symbol)
            h = grouped.setdefault(key, {"filename": filename, "symbol": symbol,
                                          "sections": [], "import_only": True})
            h["sections"].append(section)
            h["import_only"] = h["import_only"] and _is_import_only(section)
    result = []
    for i, h in enumerate(grouped.values(), 1):
        h["id"] = f"H{i:04d}"
        h["patch"] = "".join(h["sections"])
        h["context"] = "\n".join(h["sections"])
        result.append(h)
    return result


def _patch(hunks: list[dict]) -> str:
    by_file: dict[str, list[dict]] = {}
    for h in hunks:
        by_file.setdefault(h["filename"], []).append(h)
    out = []
    for filename, hs in by_file.items():
        old_exists = any(h["filename"] for h in hs)
        out.append(f"diff --git a/{filename} b/{filename}\n")
        out.append(f"--- a/{filename}\n+++ b/{filename}\n")
        out.extend(h["sections"] for h in hs)
    return "".join(x if isinstance(x, str) else "".join(x) for x in out)


def _run_tests(work: Path, problem: str, checkpoint: int,
               marker: str | None = None, timeout: int = 180,
               docker_image: str | None = None) -> dict:
    tests = SCBENCH / problem / "tests" / f"test_checkpoint_{checkpoint}.py"
    if not tests.exists():
        return {"passed": False, "status": "MISSING_TEST_SUITE", "returncode": None,
                "stdout": "", "stderr": f"Missing test suite: {tests}"}
    python = str(SCBENCH_PYTHON) if SCBENCH_PYTHON.exists() else sys.executable
    if docker_image:
        # Use the same isolated benchmark image for Prune validation.  The
        # The benchmark tests are mounted only in the validation container;
        # the Codex agent never receives this mount.
        cmd = ["docker", "run", "--rm",
               "-v", f"{work}:/work", "-v", f"{SCBENCH}:/bench",
               "-w", "/work", docker_image, "uv", "run",
               "--with", "pytest", "--with", "pytest-json-report",
               "--with", "pytest-timeout", "--with", "httpx",
               "--with", "jsonschema"]
        for dependency in _test_dependencies(problem):
            cmd.extend(["--with", dependency])
        cmd += ["python", "-m", "pytest", "-q",
               f"/bench/{tests.relative_to(SCBENCH)}"]
    else:
        cmd = [python, "-m", "pytest", "-q", str(tests)]
    if marker:
        cmd += ["-m", marker]
    entrypoint = (f"python /work/{_entrypoint_name(work)}"
                  if docker_image else f"{python} {work / _entrypoint_name(work)}")
    cmd += ["--entrypoint", entrypoint,
           "--checkpoint", f"checkpoint_{checkpoint}"]
    try:
        p = subprocess.run(cmd, cwd=work, text=True, capture_output=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return {"passed": False, "status": "TIMEOUT", "returncode": None,
                "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else ""}
    except OSError as exc:
        return {"passed": False, "status": "INFRA_ERROR", "returncode": None,
                "stdout": "", "stderr": f"Could not execute test runner: {exc}"}
    return {"passed": p.returncode == 0, "returncode": p.returncode,
            "status": "PASS" if p.returncode == 0 else "FAIL",
            "stdout": p.stdout[-12000:], "stderr": p.stderr[-4000:]}


def _entrypoint_name(work: Path) -> str:
    files = [p.name for p in work.iterdir() if p.suffix == ".py" and p.name != "conftest.py"]
    if not files:
        raise RuntimeError("snapshot has no Python entrypoint")
    return files[0]


def _test_dependencies(problem: str) -> list[str]:
    """Read benchmark-declared dependencies for Docker validation."""
    config = SCBENCH / problem / "config.yaml"
    try:
        import yaml
        data = yaml.safe_load(config.read_text()) or {}
        return [str(dep) for dep in (data.get("test_dependencies", []) or [])]
    except (OSError, ValueError, ImportError):
        return []


def _llm(feature: str, hunks: list[dict], artifact: Path, model: str) -> list[dict]:
    candidates = [h for h in hunks if not h["import_only"] and h["filename"].endswith(".py")]
    system = ("You are selecting implementation subsets for a feature patch. Return strict JSON only "
              "in this schema: {\"subsets\":[{\"hunks\":[\"H0001\"],"
              "\"rationale\":\"...\",\"dependencies\":[\"H0002\"]}]}. "
              "Return at least four DISTINCT candidate subsets when there are enough hunks: "
              "(1) the smallest plausible feature-complete subset, (2) an alternative minimal "
              "subset, (3) a conservative intermediate subset, and (4) the full non-import patch. "
              "Order them from smallest to largest. A subset must preserve all dependencies needed "
              "for its selected feature behavior. Prefer subsets that preserve existing behavior "
              "and avoid unrelated changes. Use only IDs provided; never invent IDs. "
              "Import-only changes are retained automatically and must not be listed.")
    payload = {"feature_request": feature, "hunks": [{k: h[k] for k in ("id", "filename", "symbol", "context")} for h in candidates]}
    (artifact / "llm_request.json").write_text(json.dumps({"system": system, "input": payload}, indent=2))
    try:
        from openai import OpenAI
        client = OpenAI()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload)}]
        try:
            response = client.responses.create(
                model=model, store=False, input=messages,
                text={"format": {"type": "json_object"}}, max_output_tokens=5000)
        except TypeError:
            # Older local OpenAI SDKs reject the Responses `text` keyword;
            # the prompt still requires JSON, so retry without that optional
            # structured-output parameter and validate the parsed response.
            response = client.responses.create(model=model, input=messages)
        raw = response.output_text
        (artifact / "llm_response.json").write_text(raw)
        obj = json.loads(raw)
        valid = {h["id"] for h in candidates}
        out = []
        seen = set()
        for item in obj.get("subsets", []):
            ids = tuple(sorted(set(item.get("hunks", []))))
            if not ids or not set(ids) <= valid or ids in seen:
                continue
            seen.add(ids)
            out.append({"hunks": list(ids),
                        "rationale": item.get("rationale", ""),
                        "dependencies": sorted(set(item.get("dependencies", [])))})
        full = tuple(sorted(valid))
        if full and full not in seen:
            out.append({"hunks": list(full),
                        "rationale": "Conservative baseline containing every non-import hunk.",
                        "dependencies": []})
        if out:
            return sorted(out, key=lambda x: (len(x["hunks"]), x["hunks"]))
    except Exception as exc:
        (artifact / "llm_error.txt").write_text(f"{type(exc).__name__}: {exc}")
        (artifact / "llm_status.json").write_text(json.dumps({
            "status": "LLM_FAILED", "error": f"{type(exc).__name__}: {exc}"
        }, indent=2))
    else:
        (artifact / "llm_status.json").write_text(json.dumps({
            "status": "OK", "num_subsets": len(out) if 'out' in locals() else 0
        }, indent=2))
    # A deterministic fallback still exercises the full patch/test pipeline.
    return [{"hunks": [h["id"] for h in candidates], "rationale": "fallback: full non-import patch"}]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, default=DEFAULT_RUN)
    ap.add_argument("--problem", default="execution_server")
    ap.add_argument("--from-checkpoint", type=int, default=1,
                    help="Old checkpoint/objective (default: 1).")
    ap.add_argument("--to-checkpoint", type=int, default=2,
                    help="Feature checkpoint/objective (default: 2).")
    ap.add_argument("--p2p-checkpoint", type=int,
                    help="Preservation-test checkpoint; defaults to from-checkpoint.")
    ap.add_argument("--model", default="gpt-5.6-luna")
    ap.add_argument("--output", type=Path, default=ROOT / "smoke-results/prune-codeslop-execution-server")
    ap.add_argument("--test-timeout", type=int, default=180)
    ap.add_argument("--max-pair-removals", type=int, default=64,
                    help="Bound quadratic pair-removal diagnostics.")
    ap.add_argument("--max-subsets", type=int, default=16,
                    help="Maximum LLM proposals to evaluate per objective.")
    ap.add_argument("--docker-image", default=os.environ.get("SLOPCODE_PRUNE_DOCKER_IMAGE"),
                    help="Run Prune tests in this Docker image when set.")
    args = ap.parse_args()
    if args.to_checkpoint <= args.from_checkpoint:
        ap.error("--to-checkpoint must be greater than --from-checkpoint")
    p2p_checkpoint = args.p2p_checkpoint or args.from_checkpoint
    base = args.run / args.problem
    old = _source_files(base / f"checkpoint_{args.from_checkpoint}/snapshot")
    new = _source_files(base / f"checkpoint_{args.to_checkpoint}/snapshot")
    feature = (base / f"checkpoint_{args.to_checkpoint}/prompt.txt").read_text()
    artifact = args.output; artifact.mkdir(parents=True, exist_ok=True)
    hunks = extract_hunks(old, new)
    (artifact / "hunks.json").write_text(json.dumps(hunks, indent=2))
    subsets = _llm(feature, hunks, artifact, args.model)
    if len(subsets) > args.max_subsets:
        subsets = subsets[:args.max_subsets]
        (artifact / "limits.json").write_text(json.dumps({
            "max_subsets": args.max_subsets, "subsets_truncated": True
        }, indent=2))
    (artifact / "subsets.json").write_text(json.dumps(subsets, indent=2))
    records = []
    for subset in subsets:
        selected = [h for h in hunks if h["import_only"] or h["id"] in subset["hunks"]]
        with tempfile.TemporaryDirectory(prefix="prune-codeslop-") as td:
            work = Path(td); work.mkdir(exist_ok=True)
            for name, text in old.items():
                dst = work / name; dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(text)
            subprocess.run(["git", "init", "-q"], cwd=work, check=True)
            subprocess.run(["git", "add", "."], cwd=work, check=True)
            subprocess.run(["git", "-c", "user.email=prune@example.com", "-c", "user.name=prune", "commit", "--allow-empty", "-qm", "old"], cwd=work, check=True)
            patch_path = work / "selected.patch"; patch_path.write_text(_patch(selected))
            applied = (subprocess.CompletedProcess([], 0, "", "")
                       if not selected else
                       subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=work, text=True, capture_output=True))
            if applied.returncode:
                record = {"subset": subset, "status": "PATCH_FAILED", "patch_error": applied.stderr}
            else:
                f2p = _run_tests(work, args.problem, args.to_checkpoint,
                                  "not error and not regression", args.test_timeout,
                                  args.docker_image)
                p2p = _run_tests(work, args.problem, p2p_checkpoint,
                                  timeout=args.test_timeout,
                                  docker_image=args.docker_image)
                record = {"subset": subset, "status": "PASS" if f2p["passed"] and p2p["passed"] else "FAIL", "f2p": f2p, "p2p": p2p}
                if record["status"] == "PASS":
                    final_snapshot = artifact / "final_snapshot"
                    if final_snapshot.exists():
                        shutil.rmtree(final_snapshot)
                    shutil.copytree(work, final_snapshot,
                                    ignore=shutil.ignore_patterns(".git", "selected.patch"))
                    record["final_snapshot"] = str(final_snapshot)
    
        record["counterfactuals"] = []
        # Probe every degree-1 removal even when the complete candidate fails.
        # This identifies whether one hunk is sufficient to explain the
        # regression, while imports remain retained in every probe.
        if selected:
            for removed in [h for h in selected if not h["import_only"]]:
                remaining = [h for h in selected if h is not removed]
                with tempfile.TemporaryDirectory(prefix="prune-codeslop-cf-") as ctd:
                    cwork = Path(ctd)
                    for name, text in old.items():
                        dst = cwork / name; dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(text)
                    subprocess.run(["git", "init", "-q"], cwd=cwork, check=True)
                    subprocess.run(["git", "add", "."], cwd=cwork, check=True)
                    subprocess.run(["git", "-c", "user.email=prune@example.com", "-c", "user.name=prune", "commit", "--allow-empty", "-qm", "old"], cwd=cwork, check=True)
                    patch_path = cwork / "selected.patch"; patch_path.write_text(_patch(remaining))
                    applied = (subprocess.CompletedProcess([], 0, "", "")
                               if not remaining else
                               subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=cwork, text=True, capture_output=True))
                    if applied.returncode:
                        cf = {"removed": removed["id"], "status": "PATCH_FAILED"}
                    else:
                        cf_f2p = _run_tests(cwork, args.problem, args.to_checkpoint,
                                            "not error and not regression", args.test_timeout,
                                            args.docker_image)
                        cf_p2p = _run_tests(cwork, args.problem, p2p_checkpoint,
                                            timeout=args.test_timeout,
                                            docker_image=args.docker_image)
                        cf = {"removed": removed["id"], "status": "PASS" if cf_f2p["passed"] and cf_p2p["passed"] else "FAIL", "f2p": cf_f2p, "p2p": cf_p2p}
                    record["counterfactuals"].append(cf)
            record["pair_removals"] = []
            removable = [h for h in selected if not h["import_only"]]
            for pair_index, (first, second) in enumerate(combinations(removable, 2)):
                if pair_index >= args.max_pair_removals:
                    record.setdefault("limits", {})["pair_removals"] = {
                        "max": args.max_pair_removals,
                        "truncated": True,
                    }
                    break
                remaining = [h for h in selected if h is not first and h is not second]
                with tempfile.TemporaryDirectory(prefix="prune-codeslop-pair-") as ptd:
                    pwork = Path(ptd)
                    for name, text in old.items():
                        dst = pwork / name; dst.parent.mkdir(parents=True, exist_ok=True); dst.write_text(text)
                    subprocess.run(["git", "init", "-q"], cwd=pwork, check=True)
                    subprocess.run(["git", "add", "."], cwd=pwork, check=True)
                    subprocess.run(["git", "-c", "user.email=prune@example.com", "-c", "user.name=prune", "commit", "--allow-empty", "-qm", "old"], cwd=pwork, check=True)
                    patch_path = pwork / "selected.patch"; patch_path.write_text(_patch(remaining))
                    applied = (subprocess.CompletedProcess([], 0, "", "")
                               if not remaining else
                               subprocess.run(["git", "apply", "--whitespace=nowarn", str(patch_path)], cwd=pwork, text=True, capture_output=True))
                    if applied.returncode:
                        pair_result = {"removed": [first["id"], second["id"]], "status": "PATCH_FAILED"}
                    else:
                        pair_f2p = _run_tests(pwork, args.problem, args.to_checkpoint,
                                              "not error and not regression", args.test_timeout,
                                              args.docker_image)
                        pair_p2p = _run_tests(pwork, args.problem, p2p_checkpoint,
                                              timeout=args.test_timeout,
                                              docker_image=args.docker_image)
                        pair_result = {"removed": [first["id"], second["id"]], "status": "PASS" if pair_f2p["passed"] and pair_p2p["passed"] else "FAIL", "f2p": pair_f2p, "p2p": pair_p2p}
                    record["pair_removals"].append(pair_result)
        records.append(record)
    result = {"problem": args.problem, "from_checkpoint": args.from_checkpoint,
              "to_checkpoint": args.to_checkpoint,
              "original": {"f2p": None, "p2p": None}, "hunks": hunks,
              "subsets": subsets, "evaluations": records}
    (artifact / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({"problem": args.problem, "from_checkpoint": args.from_checkpoint,
                      "to_checkpoint": args.to_checkpoint, "original_hunks": len(hunks),
                      "subsets": len(subsets),
                      "evaluations": [{"hunks": x["subset"]["hunks"], "status": x["status"]}
                                      for x in records]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
