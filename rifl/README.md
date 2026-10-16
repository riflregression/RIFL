# SafeMin

SafeMin is an alternate FeatBench pipeline. It consumes the existing Codex
FeatBench answers and evaluates LLM-proposed minimal subsets of the generated
feature patch; it does not generate a new feature patch.

The authoritative inputs are `datasets/FeatBench/dataset/featbench_v1_0.json`
for task specifications and
`datasets/FeatBench/docker_agent/results/codex_as-is_evaluation_results.json`
for the already-run F2P/P2P outcomes. By default only tasks whose original
Codex patch passed both suites are selected.

The LLM is constrained to return strict JSON in the form
`{"subsets":[{"hunks":["H0001"],"rationale":"..."}]}`. Unknown hunk IDs,
empty subsets, and malformed responses are rejected.

Changes are grouped by `(file, enclosing function/class, status)`. Import-only
groups are removed from LLM selection but automatically applied to every
candidate subset. Test-file changes are otherwise ordinary candidates. Each
subset is applied to a clean checkout of `base_commit`, first applying the
selected feature patch and then the benchmark test patch, matching FeatBench's
runner. F2P and P2P must both pass. For every passing subset,
each candidate hunk is removed once and both suites are rerun; removal of a
hunk is necessary when either suite fails. Every subset at the first passing
size is evaluated and all valid minimal subsets are reported.

Run from the repository root:

```bash
source .venv/bin/activate
OPENAI_API_KEY="$OPENAI_API_KEY" python rifl/safemin.py --limit 1 --model gpt-5.6-luna
```

Defaults are in `config/default.yaml`. Use `--config path/to/config.yaml` to
set the model, output directory, test timeout, and API-key environment-variable
name. Keep real API keys in the environment or an untracked private config.

For regression analysis of tasks whose original F2P passed but P2P failed, use
`--f2p-pass-p2p-fail`. Candidate subsets still qualify only when both F2P and
P2P pass, so this mode searches for a feature-preserving subset that repairs the
P2P regression.

Artifacts are written under `safemin-results/<instance-id>/`: extracted hunks,
filtered IDs, the exact LLM request/response, each applied subset and
counterfactual test log, and `result.json`.

## CodeSlopBench motivating smoke test

`codeslop_smoke.py` adapts the same subset logic to CodeSlopBench's
checkpoint snapshots. It treats checkpoint 1 as `R_old`, checkpoint 2 as the
new feature patch, runs checkpoint-2 feature tests as F2P, and reruns the
complete checkpoint-1 suite as P2P. The checked-in example uses
`execution_server`, whose checkpoint 2 feature tests pass while one inherited
regression test fails
(44/45), with no infrastructure failure.

```bash
source .venv/bin/activate
python model/prune-version/codeslop_smoke.py \
  --problem execution_server \
  --output smoke-results/prune-codeslop-execution-server
```

The run writes grouped hunks, the model request/response, selected subsets,
and per-suite logs to `smoke-results/prune-codeslop-execution-server/`.
