from __future__ import annotations

import argparse
import ast
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
FEAT = ROOT / "datasets" / "FeatBench"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config" / "default.yaml"
if str(FEAT) not in sys.path:
    sys.path.insert(0, str(FEAT))

from docker_agent.container.container_operator import ContainerOperator  # noqa: E402
from docker_agent.container.docker_env_manager import DockerEnvironmentManager  # noqa: E402
from docker_agent.core.base_runner import BaseRunner  # noqa: E402
from docker_agent.core.types import TestStatus  # noqa: E402


SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["subsets"],
    "properties": {"subsets": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["hunks", "rationale"],
        "properties": {"hunks": {"type": "array", "items": {"type": "string"}},
                       "rationale": {"type": "string"}}
    }}}
}


@dataclass
class Hunk:
    id: str
    filename: str
    status: str
    symbol: str
    change_type: str
    patch_sections: list[str]
    context: str
    import_only: bool = False
    is_test: bool = False
    lines: list[int] = field(default_factory=list)


class ProgressTable:
    """Small live, durable progress view shared by all pipeline stages."""
    columns = ("Task", "Stage", "Orig", "Cand", "Subsets", "Pass", "Final")

    def __init__(self, path: Path, task_ids: list[str]):
        self.path = path
        self.rows = {task: {"task": task, "stage": "queued", "original_hunks": 0,
                            "candidate_hunks": 0, "subsets_tested": 0,
                            "passing_subsets": 0, "final_minimal_hunk_counts": [], "error": ""}
                     for task in task_ids}
        self._save()

    def update(self, task: str, stage: str | None = None, **values: Any) -> None:
        row = self.rows.setdefault(task, {"task": task})
        if stage is not None:
            row["stage"] = stage
        row.update(values)
        self._save()
        self.render()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {"updated_at": time.time(), "tasks": list(self.rows.values())}
        self.path.write_text(json.dumps(snapshot, indent=2))
        for task, row in self.rows.items():
            task_dir = self.path.parent / task
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "progress.json").write_text(json.dumps(row, indent=2))

    def render(self) -> None:
        widths = [34, 18, 5, 5, 8, 6, 12]
        print("\nSafeMin pipeline progress", flush=True)
        print("  ".join(name.ljust(width) for name, width in zip(self.columns, widths)), flush=True)
        print("-" * (sum(widths) + 2 * (len(widths) - 1)), flush=True)
        for row in self.rows.values():
            final = ",".join(str(x) for x in row.get("final_minimal_hunk_counts", [])) or "-"
            values = (row.get("task", ""), row.get("stage", "-"), row.get("original_hunks", 0),
                      row.get("candidate_hunks", 0), row.get("subsets_tested", 0),
                      row.get("passing_subsets", 0), final)
            print("  ".join(str(value)[:width].ljust(width) for value, width in zip(values, widths)), flush=True)


def _sections(patch: str) -> list[str]:
    lines = patch.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("@@")]
    return ["\n".join(lines[s:(starts[n + 1] if n + 1 < len(starts) else len(lines))])
            for n, s in enumerate(starts)]


def _changed_lines(section: str) -> list[str]:
    return [x[1:] for x in section.splitlines()[1:] if x.startswith(("+", "-")) and not x.startswith(("+++", "---"))]


def _symbol(section: str, source: str = "") -> tuple[str, str]:
    # Unified-diff context normally contains the enclosing definition. Prefer it.
    text = "\n".join(section.splitlines()[1:])
    defs = re.findall(r"^[ +\-]*(async\s+def|def|class)\s+([A-Za-z_][\w]*)", text, re.M)
    if defs:
        kind, name = defs[-1]
        return name, "class" if kind == "class" else "function"
    if source:
        # Best-effort AST lookup by the old/new hunk line range.
        try:
            tree = ast.parse(source)
            line = int(re.match(r"@@ -(\d+)", section).group(1))
            candidates = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                         and n.lineno <= line <= getattr(n, "end_lineno", n.lineno)]
            if candidates:
                n = max(candidates, key=lambda x: x.lineno)
                return n.name, "class" if isinstance(n, ast.ClassDef) else "function"
        except Exception:
            pass
    return "<file-scope>", "statement"


def _definition_context(source: str, symbol: str) -> str:
    if not source or symbol == "<file-scope>":
        return ""
    try:
        tree = ast.parse(source)
        nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol]
        if nodes:
            node = min(nodes, key=lambda n: n.lineno)
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1:getattr(node, "end_lineno", node.lineno)])
    except (SyntaxError, ValueError):
        pass
    return ""


def extract_hunks(spec: dict[str, Any], artifact: Path, source_map: dict[str, str] | None = None) -> list[Hunk]:
    """Extract physical diff hunks, then group all sections in one symbol."""
    grouped: dict[tuple[str, str, str], Hunk] = {}
    number = 1
    for file_change in spec.get("patch") or []:
        filename = file_change.get("filename", "")
        raw = file_change.get("patch", "")
        sections = _sections(raw)
        if not sections:
            continue
        is_test = bool(re.search(r"(^|/)(test|tests|testing)(/|$)|(^|/)test[^/]*\.py$|_test\.py$", filename, re.I))
        source = (source_map or {}).get(filename, "")
        for section in sections:
            changed = _changed_lines(section)
            if not changed:
                continue
            import_only = all(re.match(r"\s*(from\s+\S+\s+import|import)\b", x) for x in changed)
            symbol, kind = _symbol(section, source)
            key = (filename, symbol, file_change.get("status", "modified"))
            if key not in grouped:
                grouped[key] = Hunk(
                    id=f"H{number:04d}", filename=filename, status=file_change.get("status", "modified"),
                    symbol=symbol, change_type=kind, patch_sections=[], context="", import_only=import_only, is_test=is_test)
                number += 1
            h = grouped[key]
            h.patch_sections.append(section)
            h.import_only = h.import_only and import_only
            h.context += section + "\n"
    hunks = list(grouped.values())
    for h in hunks:
        definition = _definition_context((source_map or {}).get(h.filename, ""), h.symbol)
        if definition:
            h.context = "[ENCLOSING DEFINITION]\n" + definition + "\n[HUNK DIFF]\n" + h.context
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "hunks.json").write_text(json.dumps([asdict(h) for h in hunks], indent=2))
    return hunks


def _load_generated_patch(spec: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    """Load the Codex patch saved by the existing FeatBench answer runner."""
    rel = row.get("codex_run_dir")
    if not rel:
        return []
    candidates = [FEAT / "docker_agent" / "swap" / rel / "patch.diff",
                  FEAT / "docker_agent" / rel / "patch.diff",
                  ROOT / rel / "patch.diff"]
    patch_path = next((p for p in candidates if p.is_file()), None)
    if patch_path is None:
        raise FileNotFoundError(f"Codex patch not found for {spec['instance_id']}; checked: {candidates}")
    from docker_agent.parsing.patch_analyzer import PatchAnalyzer
    parsed = PatchAnalyzer().parse_unified_diff(patch_path.read_text(encoding="utf-8", errors="replace"))
    return [{"filename": p.filename, "status": p.status, "patch": p.patch_content} for p in parsed]


def _patch_changes(hunks: Iterable[Hunk]) -> list[dict[str, Any]]:
    by_file: dict[tuple[str, str], list[Hunk]] = {}
    for h in hunks:
        by_file.setdefault((h.filename, h.status), []).append(h)
    out = []
    for (filename, status), hs in by_file.items():
        out.append({"filename": filename, "status": status,
                    "patch": "\n".join(s for h in hs for s in h.patch_sections)})
    return out


def _tests(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(f"expected_{key}_tests")
    if value is None:
        # FeatBench specs store the same lists as comma-separated
        # FAIL_TO_PASS/PASS_TO_PASS fields; evaluation result rows expose the
        # normalized expected_*_tests arrays.
        value = row.get("FAIL_TO_PASS" if key == "f2p" else "PASS_TO_PASS") or []
    if isinstance(value, str):
        return [x.strip() for x in value.split(",") if x.strip()]
    return list(value)


def _load_config(path: Path, require_key: bool = True) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for SafeMin configuration") from exc
    if not path.exists():
        raise FileNotFoundError(f"SafeMin config does not exist: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError("SafeMin config must contain a YAML mapping")
    api_env = data.get("openai_api_key_env", "OPENAI_API_KEY")
    configured_key = str(data.get("openai_api_key") or "")
    if configured_key and not configured_key.startswith("#Your_Key_here") and not os.environ.get(api_env):
        os.environ[api_env] = configured_key
    if require_key and not os.environ.get(api_env):
        raise RuntimeError(f"OpenAI API key is missing; set ${api_env} or provide openai_api_key in the config")
    data["model"] = data.get("model", "gpt-5.6-luna")
    data["output_dir"] = data.get("output_dir", "smoke-results/safemin")
    data["test_timeout"] = int(data.get("test_timeout", 1200))
    return data


class SafeMin:
    def __init__(self, output: Path, model: str = "gpt-5.6-luna", timeout: int = 1200, progress: ProgressTable | None = None):
        self.output = output; self.model = model; self.timeout = timeout
        self.manager = DockerEnvironmentManager()
        self.base = BaseRunner()
        # The progress table is the user-facing stream; detailed Docker and
        # pytest logs remain available in per-node artifacts.
        logging.getLogger().setLevel(logging.WARNING)
        self.progress = progress

    def _stage(self, task: str, stage: str, **values: Any) -> None:
        if self.progress:
            self.progress.update(task, stage, **values)

    def _llm(self, spec: dict[str, Any], hunks: list[Hunk], artifact: Path) -> list[dict[str, Any]]:
        prompt = {
            "feature_request": spec.get("problem_statement", ""),
            "hunks": [{"id": h.id, "file": h.filename, "symbol": h.symbol,
                       "change_type": h.change_type, "is_test": h.is_test,
                       "context": h.context} for h in hunks if not h.import_only]
        }
        system = ("You select minimal subsets of already-generated feature hunks. "
                  "A subset is valid only if implemented on the old repository it applies both the new feature as well as retain existing functionalities. "
                  "Return strict JSON matching the schema: {subsets:[{hunks:[\"H0001\"],rationale:\"...\"}]}. "
                  "Return at least four distinct alternatives when enough hunks exist: a minimal feature "
                  "subset, a P2P-preserving alternative, an intermediate subset, and a conservative full subset. "
                  "Use only provided hunk IDs, never invent IDs, and include dependency hunks together when needed.")
        artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "llm_request.json").write_text(json.dumps({"system": system, "input": prompt}, indent=2))
        from openai import OpenAI
        client = OpenAI()
        request = {"model": self.model, "store": False, "reasoning": {"effort": "low"},
                   "input": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(prompt)}],
                   "text": {"format": {"type": "json_schema", "name": "safemin_subsets", "strict": True, "schema": SCHEMA}},
                   "max_output_tokens": 12000}
        response = client.responses.create(**request)
        raw = response.output_text
        (artifact / "llm_response.json").write_text(raw)
        obj = json.loads(raw)
        valid = {h.id for h in hunks if not h.import_only}
        subsets = []
        for item in obj.get("subsets", []):
            ids = sorted(set(item.get("hunks", [])), key=lambda x: int(x[1:]) if x[1:].isdigit() else 10**9)
            if ids and all(x in valid for x in ids):
                subsets.append({"hunks": ids, "rationale": item.get("rationale", "")})
        return sorted(subsets, key=lambda x: (len(x["hunks"]), x["hunks"]))

    def _llm_refine(self, spec: dict[str, Any], hunks: list[Hunk], artifact: Path,
                    proposed: list[dict[str, Any]], invalid: list[dict[str, Any]],
                    attempt: int) -> list[dict[str, Any]]:
        """Ask for new subsets after execution invalidated all prior proposals."""
        valid_ids = {h.id for h in hunks if not h.import_only}
        def _test_set(value: Any) -> set[str]:
            if isinstance(value, str):
                return {x.strip() for x in value.split(",") if x.strip()}
            return {str(x) for x in (value or [])}
        expected_f = _test_set(spec.get("expected_f2p_tests"))
        expected_p = _test_set(spec.get("expected_p2p_tests"))
        failures = []
        for item in invalid:
            result = item.get("result", {})
            failures.append({
                "subset": item.get("subset", {}).get("hunks", []),
                "status": result.get("status"),
                "f2p_passed": result.get("f2p"),
                "p2p_passed": result.get("p2p"),
                "failing_f2p_tests": sorted(expected_f - set(result.get("passed_f2p", []))),
                "failing_p2p_tests": sorted(expected_p - set(result.get("passed_p2p", []))),
            })
        prompt = {"feature_request": spec.get("problem_statement", ""),
                  "hunks": [{"id": h.id, "file": h.filename, "symbol": h.symbol,
                             "context": h.context} for h in hunks if not h.import_only],
                  "previous_subsets": proposed,
                  "invalidated_results": failures,
                  "instruction": "Propose different dependency-aware subsets. Do not repeat prior subsets. "
                  "Preserve hunks needed by passing F2P tests, and specifically change the hunk combination "
                  "that may be responsible for the listed failing P2P tests."}
        system = ("Iteratively refine feature hunk subsets. Every prior subset failed execution. "
                  "Return strict JSON {subsets:[{hunks:[\"H0001\"],rationale:\"...\"}]}. "
                  "Use only supplied IDs, return distinct alternatives, and avoid every previously "
                  "invalidated subset. Include dependencies together.")
        path = artifact / f"llm_refinement_{attempt}.json"
        path.write_text(json.dumps({"system": system, "input": prompt}, indent=2))
        from openai import OpenAI
        response = OpenAI().responses.create(
            model=self.model, store=False, reasoning={"effort": "low"},
            input=[{"role": "system", "content": system},
                   {"role": "user", "content": json.dumps(prompt)}],
            text={"format": {"type": "json_schema", "name": "safemin_refinement",
                              "strict": True, "schema": SCHEMA}}, max_output_tokens=12000)
        raw = response.output_text
        (artifact / f"llm_refinement_{attempt}_response.json").write_text(raw)
        obj = json.loads(raw); seen = set(); result = []
        for item in obj.get("subsets", []):
            ids = sorted(set(item.get("hunks", [])), key=lambda x: int(x[1:]) if x[1:].isdigit() else 10**9)
            key = tuple(ids)
            if ids and key not in seen and all(x in valid_ids for x in ids):
                seen.add(key); result.append({"hunks": ids, "rationale": item.get("rationale", ""), "refinement_attempt": attempt})
        return sorted(result, key=lambda x: (len(x["hunks"]), x["hunks"]))

    def _run_pair(self, op: ContainerOperator, spec: dict[str, Any], patch: list[dict[str, Any]], artifact: Path, label: str) -> dict[str, Any]:
        rendered = []
        for change in patch:
            filename, status, content = change.get("filename", ""), change.get("status", "modified"), change.get("patch", "")
            header = f"diff --git a/{filename} b/{filename}\n"
            if status == "added": header += f"new file mode 100644\n--- /dev/null\n+++ b/{filename}\n"
            elif status == "removed": header += f"deleted file mode 100644\n--- a/{filename}\n+++ /dev/null\n"
            else: header += f"--- a/{filename}\n+++ b/{filename}\n"
            rendered.append(header + content + "\n")
        (artifact / f"{label}.patch.diff").write_text("".join(rendered))
        op.checkout_commit(spec["base_commit"])
        if patch:
            applied = op.apply_patches(patch)
            expected_files = {x["filename"] for x in patch}
            if expected_files - set(applied):
                return {"status": "PATCH_FAILED", "applied": applied, "f2p": False, "p2p": False}
        # Match the FeatBench runner: apply the generated feature patch first,
        # then the hidden benchmark tests.
        if spec.get("test_patch"): op.apply_patches(spec["test_patch"])
        f2p = _tests(spec, "f2p"); p2p = _tests(spec, "p2p")
        fpass, flog = op.run_tests_in_container(spec["repo"].split("/")[-1], f2p, [TestStatus.PASSED], use_xdist=False) if f2p else (set(), "")
        ppass, plog = op.run_tests_in_container(spec["repo"].split("/")[-1], p2p, [TestStatus.PASSED], use_xdist=False) if p2p else (set(), "")
        result = {"status": "PASS" if set(f2p) <= fpass and set(p2p) <= ppass else "FAIL",
                  "f2p": set(f2p) <= fpass, "p2p": set(p2p) <= ppass,
                  "passed_f2p": sorted(fpass), "passed_p2p": sorted(ppass),
                  "f2p_log": flog, "p2p_log": plog}
        (artifact / f"{label}.json").write_text(json.dumps(result, indent=2, default=list))
        return result

    def run_task(self, spec: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        # Keep the dataset spec (patches, base commit, feature request) and
        # overlay the authoritative test lists from the existing evaluation
        # answer. This avoids rerunning or guessing benchmark selection.
        spec = dict(spec)
        spec["expected_f2p_tests"] = row.get("expected_f2p_tests") or spec.get("FAIL_TO_PASS") or []
        spec["expected_p2p_tests"] = row.get("expected_p2p_tests") or spec.get("PASS_TO_PASS") or []
        # The dataset spec intentionally does not contain the agent's answer
        # patch. It is authoritative in the existing Codex result's run
        # directory, so load and normalize it before hunk extraction.
        spec["patch"] = _load_generated_patch(spec, row)
        task_id = spec["instance_id"]
        self._stage(task_id, "container")
        container = self.manager.create_container(self.base._dict_to_spec(spec))
        op = ContainerOperator(spec["repo"], container)
        artifact = self.output / spec["instance_id"]; artifact.mkdir(parents=True, exist_ok=True)
        (artifact / "spec.json").write_text(json.dumps(spec, indent=2))
        # Read old source from the task image so statement hunks can be
        # grouped by their actual enclosing function/class, even when the
        # unified-diff function-context suffix is absent.
        source_map: dict[str, str] = {}
        op.checkout_commit(spec["base_commit"])
        self._stage(task_id, "extracting")
        for change in spec.get("patch") or []:
            filename = change.get("filename", "")
            code, text = op.docker_executor.execute(f"git show {spec['base_commit']}:{filename}", f"/workdir/swap/{spec['repo'].split('/')[-1]}", tty=False)
            if code == 0: source_map[filename] = text or ""
        hunks = extract_hunks(spec, artifact, source_map)
        candidates = [h for h in hunks if not h.import_only]
        imports = [h for h in hunks if h.import_only]
        self._stage(task_id, "LLM subset planning", original_hunks=len(hunks), candidate_hunks=len(candidates))
        (artifact / "filtered_hunks.json").write_text(json.dumps({"candidate_ids": [h.id for h in candidates], "import_only_ids": [h.id for h in imports]}, indent=2))
        # LLM planning happens before the normal subset-evaluation finally
        # block. Clean up here too, so authentication/API failures never leave
        # a pulled task image occupying disk space.
        try:
            subsets = self._llm(spec, hunks, artifact)
        except Exception:
            self.manager.cleanup_container(container, force_remove=True)
            self.manager.cleanup_task_image(self.base._dict_to_spec(spec))
            raise
        (artifact / "subsets.json").write_text(json.dumps(subsets, indent=2))
        self._stage(task_id, "subset testing", subsets_tested=0, passing_subsets=0)
        records = []; final = []; all_subsets = list(subsets); refinement_attempts = 0
        try:
            for attempt in range(3):
                if attempt > 0:
                    invalid = [{"subset": r["subset"], "result": r["result"]}
                               for r in records if r.get("result", {}).get("status") != "PASS"]
                    try:
                        subsets = self._llm_refine(spec, hunks, artifact, all_subsets, invalid, attempt)
                    except Exception as exc:
                        (artifact / f"llm_refinement_{attempt}_error.txt").write_text(f"{type(exc).__name__}: {exc}")
                        subsets = []
                    refinement_attempts = attempt
                    existing = {tuple(x["hunks"]) for x in all_subsets}
                    subsets = [x for x in subsets if tuple(x["hunks"]) not in existing]
                    all_subsets.extend(subsets)
                if not subsets:
                    continue
                sizes = sorted({len(x["hunks"]) for x in subsets})
                for size in sizes:
                  for ordinal, subset in enumerate([x for x in subsets if len(x["hunks"]) == size], 1):
                    selected = imports + [h for h in candidates if h.id in subset["hunks"]]
                    result = self._run_pair(op, spec, _patch_changes(selected), artifact, f"subset-{size}-{ordinal}")
                    rec = {"subset": subset, "result": result, "counterfactuals": []}
                    if result.get("status") == "PASS":
                        for h in selected:
                            if h.import_only: continue
                            reduced = [x for x in selected if x.id != h.id]
                            cf = self._run_pair(op, spec, _patch_changes(reduced), artifact, f"subset-{size}-{ordinal}-remove-{h.id}")
                            rec["counterfactuals"].append({"removed": h.id, "result": cf})
                        rec["valid_minimal"] = all(x["result"].get("status") != "PASS" for x in rec["counterfactuals"])
                        if rec["valid_minimal"]: final.append(subset)
                    records.append(rec)
                    self._stage(task_id, "counterfactuals" if result.get("status") == "PASS" else "subset testing",
                               original_hunks=len(hunks), candidate_hunks=len(candidates),
                               subsets_tested=len(records), passing_subsets=sum(r.get("result", {}).get("status") == "PASS" for r in records),
                               final_minimal_hunk_counts=sorted({len(x["hunks"]) for x in final}))
                  if final:
                    break
                if final:
                    break
            if not final and refinement_attempts >= 2:
                (artifact / "classification.json").write_text(json.dumps({
                    "status": "LLM_COULD_NOT_FIND_VALID_SUBSET",
                    "refinement_attempts": refinement_attempts + 1
                }, indent=2))
        finally:
            self.manager.cleanup_container(container, force_remove=True)
            self.manager.cleanup_task_image(self.base._dict_to_spec(spec))
        output = {"instance_id": spec["instance_id"], "hunks": [asdict(h) for h in hunks], "subsets": all_subsets,
                  "evaluations": records, "final_minimal_subsets": final,
                  "refinement_attempts": refinement_attempts + 1,
                  "classification": "PASS" if final else "LLM_COULD_NOT_FIND_VALID_SUBSET"}
        (artifact / "result.json").write_text(json.dumps(output, indent=2, default=list))
        self._stage(task_id, "complete", original_hunks=len(hunks), candidate_hunks=len(candidates),
                    subsets_tested=len(records), passing_subsets=sum(r.get("result", {}).get("status") == "PASS" for r in records),
                    final_minimal_hunk_counts=sorted({len(x["hunks"]) for x in final}))
        return output


def main() -> int:
    ap = argparse.ArgumentParser(description="SafeMin minimal FeatBench subset pipeline")
    ap.add_argument("--task-id", action="append"); ap.add_argument("--limit", type=int)
    ap.add_argument("--offset", type=int, default=0); ap.add_argument("--output", type=Path)
    ap.add_argument("--model"); ap.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ap.add_argument("--all-passed", action="store_true", help="select every task whose original F2P and P2P passed")
    ap.add_argument("--f2p-pass-p2p-fail", action="store_true", help="select tasks whose original F2P passed and P2P failed")
    ap.add_argument("--resume", action="store_true", help="skip tasks with an existing result.json in SafeMin outputs")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    config = _load_config(args.config, require_key=not args.dry_run)
    output = args.output or Path(config["output_dir"])
    model = args.model or config["model"]
    raw_dataset = json.loads((FEAT / "docker_agent" / "results" / "codex_as-is_evaluation_results.json").read_text())
    # A Codex instance can have multiple attempts. Cohort selection must use
    # the final saved attempt, not an earlier attempt that later failed/passed.
    latest_by_instance = {}
    for row in raw_dataset:
        latest_by_instance[row["instance_id"]] = row
    dataset = list(latest_by_instance.values())
    specs = {x["instance_id"]: x for group in BaseRunner()._load_specs().values() for x in group}
    if args.f2p_pass_p2p_fail:
        rows = [x for x in dataset if x.get("success_f2p") is True and x.get("success_p2p") is False]
    else:
        rows = [x for x in dataset if x.get("success_f2p") is True and x.get("success_p2p") is True]
    if args.task_id: rows = [x for x in rows if x.get("instance_id") in args.task_id]
    unique_rows = []
    seen_ids = set()
    for row in rows:
        if row.get("instance_id") not in seen_ids:
            unique_rows.append(row)
            seen_ids.add(row.get("instance_id"))
    rows = unique_rows
    if args.resume:
        completed = set()
        for candidate_root in [output, ROOT / "smoke-results" / "safemin", ROOT / "smoke-results" / "safemin-10", ROOT / "smoke-results" / "safemin-one"]:
            if candidate_root.exists():
                completed.update(p.parent.name for p in candidate_root.glob("*/result.json"))
        rows = [x for x in rows if x.get("instance_id") not in completed]
    rows = rows[args.offset: args.offset + args.limit if args.limit else None]
    progress = ProgressTable(output / "progress.json", [x["instance_id"] for x in rows])
    progress.render()
    pipe = SafeMin(output, model, timeout=config["test_timeout"], progress=progress)
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row['instance_id']} extracting/subset-testing", flush=True)
        if args.dry_run:
            continue
        if row["instance_id"] not in specs:
            print(f"  skipped: no FeatBench spec", flush=True); continue
        try:
            result = pipe.run_task(specs[row["instance_id"]], row)
            print(f"  complete: {len(result['hunks'])} hunks, {len(result['final_minimal_subsets'])} minimal subsets", flush=True)
        except Exception as exc:
            (output / row["instance_id"] / "error.txt").parent.mkdir(parents=True, exist_ok=True)
            (output / row["instance_id"] / "error.txt").write_text(f"{type(exc).__name__}: {exc}")
            progress.update(row["instance_id"], "error", error=f"{type(exc).__name__}: {exc}")
            print(f"  error: {type(exc).__name__}: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
