# RIFL reproduction package

Coding agents can successfully implement repository-level feature requests while still introducing regressions into previously correct behavior, creating a need for post-hoc localization of the generated changes responsible for those failures. We formulate this problem as Regression-Inducing Hunk Localization (RIHL) and introduce RIFL (stands for Regression-Inducing change localization in Feature impLementations). RIFL uses an LLM to propose batches of retained-hunk subsets, validates candidates in parallel with separate feature and regression test suites, and uses the aggregate execution outcomes and original proposal rationales as feedback for subsequent batches. Once a valid candidate is found, RIFL applies a Single-Hunk Reduction Check to determine whether one additional retained hunk can be removed while preserving both behavioral requirements. We evaluate RIFL against single-shot and feedback-based localization baselines across multiple trials and multiple base models. RIFL achieves 69.84% average localization success, outperforming the strongest baseline at 42.46%, with ablations confirming the contribution of its key components.

## Contents

```
reproduction/
├── rifl/                         # method implementation
│   ├── safemin.py                # FeatBench pipeline and subset search
│   ├── codeslop_smoke.py         # CodeSlopBench checkpoint adapter
│   ├── slopcode_pipeline.py      # objective-level runner
│   ├── slopcode_prune_hook.py    # Codex → RIFl interleaving hook
│   ├── config/default.yaml       # sanitized configuration
│   ├── tests/                    # unit tests
│   └── *.md                      # architecture and experiment notes
├── datasets/
│   ├── FeatBench/                # evaluator, Docker runner, and 156 tasks
│   │   └── dataset/              # public no-answer data and offline gold data
│   └── slop-code-bench/          # CodeSlopBench problems/configuration/source
└── scripts/                      # convenience launchers
```

Generated Docker containers, repositories, Codex transcripts, evaluation
outputs, virtual environments, and credentials are intentionally not included.
They are created locally while running the commands below. The package contains
no API key or personal filesystem path. Set `OPENAI_API_KEY` in the shell; do
not put a real key in a checked-in file.

## Architecture

RIFl is a post-hoc selector over an already-generated feature patch. It does not
ask the model to implement the feature again.

1. **Task loader:** reads a FeatBench feature request, base commit, test patch,
   benchmark test identifiers, and the corresponding Codex result.
2. **Patch loader:** reads Codex's `patch.diff` from the evaluator artifact.
3. **Hunk extractor:** parses unified diff sections and groups sections that
   modify the same file and enclosing function/class. Import-only groups are
   retained as baseline context but are not selected by the model; test-file
   changes remain candidates.
4. **Subset planner:** sends the feature request and each candidate hunk's
   enclosing definition plus diff context to `gpt-5.6-luna`. The model returns
   strict JSON subsets of hunk IDs and rationales.
5. **Execution validator:** starts from the old commit, applies selected
   feature hunks plus imports, applies the benchmark test patch, and runs F2P
   and P2P. A subset is valid only when both suites pass.
6. **Counterfactual minimizer:** for every passing subset, removes each selected
   hunk once and reruns both suites. A hunk is necessary when either suite
   fails after its removal. All valid subsets at the first passing size are
   reported.
7. **Refinement loop:** if no proposed subset passes, the model receives the
   feature request, hunks, prior subsets, and exact failed test IDs. It gets two
   further attempts (three attempts total), ending in a certified subset or
   `LLM_COULD_NOT_FIND_VALID_SUBSET`.
8. **Artifact/progress writer:** each task records hunks, prompts, responses,
   patches, logs, counterfactuals, classification, and `result.json`; a live
   table is stored in `progress.json`.

The CodeSlopBench adapter inserts the same selector and validator between
objectives: Codex produces a checkpoint, RIFl evaluates/minimizes it, and the
surviving snapshot becomes the next objective's input. Docker containers and
task image tags are removed after each task/checkpoint.

## Installation

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r datasets/FeatBench/requirements.txt
python -m pip install openai pyyaml pytest
export OPENAI_API_KEY='#Your_Key_here'
```

Replace the example value with a real key at run time. A Docker daemon is
required. Task images are pulled on demand, so all 156 images need not be kept
simultaneously.

## Reproduction steps

### 1. Generate baseline Codex artifacts

Run the FeatBench runner from `datasets/FeatBench` using the public
`dataset/featbench_v1_0_no_answers.json`. Keep the resulting Codex patch
directories and evaluation JSON at:

```
datasets/FeatBench/docker_agent/swap/codex-runs/as-is/<instance>/.../patch.diff
datasets/FeatBench/docker_agent/results/codex_as-is_evaluation_results.json
```

Codex must see only the old repository and feature request; reference patches
and hidden tests remain evaluator-only.

### 2. Run RIFl on passing baseline tasks

```bash
cd reproduction
. .venv/bin/activate
OPENAI_API_KEY="$OPENAI_API_KEY" python rifl/safemin.py \
  --config rifl/config/default.yaml --all-passed \
  --output results/featbench-all
```

Use `--f2p-pass-p2p-fail` for regression-repair cohorts, `--resume` after an
interruption, `--task-id <owner__repo-number>` for one task, and `--limit` or
`--offset` for a cohort.

### 3. Run CodeSlopBench

```bash
cd reproduction
OPENAI_API_KEY="$OPENAI_API_KEY" python rifl/codeslop_smoke.py \
  --problem execution_server --output results/slopcode-execution-server
```

For objective-level interleaving, configure the CodeSlopBench runner's
`SLOPCODE_POST_CHECKPOINT_HOOK` as described in `rifl/PRUNE_OVERVIEW.md`.

### 4. Inspect results

Each task's `result.json` contains grouped hunks, model subsets, F2P/P2P
outcomes, counterfactuals, refinement attempts, and final classification.
`progress.json` is a durable machine-readable progress table.

## Dataset provenance and licensing

`datasets/FeatBench/dataset/featbench_v1_0_no_answers.json` is the runtime
evaluator input. `featbench_v1_0.json` is included for offline analysis and
contains reference patch fields; do not mount it into the Codex sandbox.
CodeSlopBench problem definitions and configuration are under
`datasets/slop-code-bench/.scbench/problems` and `configs`.

The copied datasets and evaluator retain their upstream license/attribution
files. The RIFl code is provided for anonymous reproduction.
