# PRUNE: Overview and Figure-Ready Architecture

## Overview

PRUNE is a post-generation patch validation and reduction system for coding-agent
benchmarks. It does not ask the coding agent to implement a feature. The coding
agent first produces a candidate repository change. PRUNE then determines which
parts of that change are needed to implement the requested feature while
preserving behavior that existed before the change.

The central idea is to treat a generated patch as a set of semantic hunks rather
than as one indivisible diff. A hunk may be a complete function or class, or one
or more statements changed inside an existing function. Multiple physical diff
regions belonging to the same function are grouped into one hunk. Import-only
changes are retained as repository context and are not offered as independent
choices to the language model.

PRUNE gives the feature request and the hunk descriptions to an LLM. The LLM
returns one or more dependency-aware candidate subsets. PRUNE applies each
candidate to a clean copy of the old repository and executes two benchmark
suites:

- **F2P (fail-to-pass):** tests that should pass because of the new feature.
- **P2P (pass-to-pass):** tests that passed before the feature and must remain
  passing after the change.

A candidate is behaviorally valid only when both suites pass. For a valid
candidate, PRUNE performs counterfactual removal checks: it removes one selected
hunk at a time, reapplies the remaining changes to a fresh old repository, and
reruns F2P and P2P. If either suite fails after a removal, that hunk is treated
as necessary for that candidate.

The system produces an auditable record: source and diff context, LLM request and
response, candidate patches, test outputs, removal results, classification, and
the final repository snapshot. In the interleaved CodeSlopBench mode, the
validated snapshot is restored into the live coding-agent session before the
next objective starts.

## Architecture at a glance

```text
Task specification + old repository + agent patch
                         |
                         v
                 Task/run selector
                         |
                         v
                Patch and test loader
                         |
                         v
              Hunk extractor and grouper
                         |
                         v
             Context/package construction
                         |
                         v
                  LLM subset planner
                         |
                         v
             Candidate subset validator
                 /                  \
          F2P/P2P fail            F2P/P2P pass
               |                       |
             discard          counterfactual removal
                                       |
                                       v
                           classification + snapshot
                                       |
                                       v
                              artifacts and cleanup
```

The interleaved CodeSlopBench variant inserts this loop between objectives:

```text
Objective N Codex solve
        -> Codex snapshot/evaluation
        -> PRUNE N-1 -> N
        -> optional validated snapshot restoration
        -> Docker cleanup
        -> Objective N+1
```

## Components and data contracts

### 1. Task and run selector

**Purpose:** Select the benchmark instances and objective/checkpoint pair to
analyze.

**Inputs:** benchmark configuration, task IDs, optional cohort filters, saved
agent result directory, model/configuration file.

**Outputs:** a normalized task record containing task ID, repository identity,
base commit, feature request, old/new snapshots, test-suite locations, and the
agent patch location.

The selector also enforces the experiment cohort. For a standard SafeMin run,
the original patch must have passed both F2P and P2P. A separate analysis mode
can select patches where F2P passed but P2P failed to measure regression repair.

### 2. Repository and benchmark loader

**Purpose:** Establish the immutable old-repository baseline and load the
benchmark tests.

**Inputs:** repository URL or local checkout, base commit, feature request,
agent-generated patch, F2P test patch, P2P test inventory, task environment
configuration.

**Outputs:** a clean old repository `R_old`, normalized feature patch, F2P
tests, P2P tests, and an isolated execution configuration.

The coding agent does not receive the benchmark tests or ground-truth solution.
The validation process mounts them only inside the evaluator/container used by
PRUNE.

### 3. Patch parser and hunk grouper

**Purpose:** Convert a physical unified diff into semantically meaningful
candidate units.

**Inputs:** normalized patch and source files from `R_old`.

**Outputs:** an ordered hunk table. Each record contains:

```text
Hunk ID
target file
enclosing function/class/file scope
change status (added, modified, deleted)
change type (function, class, statement, text)
physical diff sections
source/diff context
import-only flag
test-file flag
```

All diff sections in one function or class are grouped into one candidate hunk.
Deleted functions/classes retain their complete deletion diff. Import-only hunks
are marked but automatically retained when a candidate patch is built.

### 4. Context and prompt builder

**Purpose:** Give the LLM enough information to reason about dependencies while
keeping the answer machine-readable.

**Inputs:** feature request and hunk table.

**Outputs:** a recorded LLM request containing strict JSON instructions and the
context for every eligible hunk.

For a whole-function/class change, the complete definition and diff are used.
For a statement change, the enclosing function plus the `+`/`-` diff statements
are used. The prompt asks for multiple distinct subsets, including a minimal
plausible subset, an alternative, an intermediate subset, and a conservative
full-patch subset. The model may annotate dependencies and rationale, but it may
only reference known hunk IDs.

### 5. LLM subset planner

**Purpose:** Propose a small, dependency-aware search space; it is not the
correctness oracle.

**Inputs:** feature request, hunk IDs, symbols, change types, and context.

**Outputs:** strict JSON containing candidate subsets, rationales, and optional
dependency annotations. Unknown IDs, malformed JSON, duplicate subsets, and
empty selections are rejected. A full non-import baseline is added when the
model omits one. LLM/API errors are recorded explicitly rather than being
silently confused with test failures.

### 6. Candidate patch builder

**Purpose:** Materialize one proposed subset as an executable patch.

**Inputs:** `R_old`, selected hunk IDs, and automatically retained import hunks.

**Outputs:** a candidate repository and the exact `selected.patch` artifact.

Every candidate starts from a fresh copy of `R_old`. This prevents state,
generated files, or dependencies from one candidate contaminating another.
Empty diffs are treated as no-op candidates rather than sent to `git apply`.

### 7. Isolated F2P/P2P validator

**Purpose:** Determine whether a candidate implements the feature and preserves
existing behavior.

**Inputs:** candidate repository, F2P/P2P tests, task environment, Docker image,
test timeout.

**Outputs:** per-suite pass/fail status, exit code, test output, timeout or
infrastructure classification, and test counts.

Validation runs inside the benchmark Docker image. A candidate is classified as
`PASS` only if both F2P and P2P pass. Missing tests, Docker failures, and
timeouts are stored as infrastructure statuses and are not mislabeled as
behavioral regressions.

### 8. Counterfactual removal validator

**Purpose:** Test whether individual selected hunks are necessary.

**Inputs:** a passing candidate subset and one hunk to remove.

**Outputs:** a fresh candidate containing all selected changes except the removed
hunk, plus new F2P/P2P results.

If either suite fails after removal, the removed hunk is necessary for that
candidate. Pair-removal diagnostics may additionally test selected hunk pairs,
subject to a configurable budget. These results explain dependencies and are
stored even when the original candidate fails.

### 9. Classifier and snapshot manager

**Purpose:** Produce the final decision and, when valid, a reusable repository
snapshot.

**Inputs:** subset test results and counterfactual results.

**Outputs:** candidate classification (`PASS`, `FAIL`, `PATCH_FAILED`,
`TIMEOUT`, or `INFRA_ERROR`), necessary-hunk evidence, final snapshot path, and
structured `result.json`.

In interleaved mode, a passing snapshot is restored exactly into the live agent
workspace before the next objective. The workspace is reset first so files
removed by PRUNE cannot remain from the Codex snapshot. Configured resume/setup
commands are then run.

### 10. Artifact store and cleanup manager

**Purpose:** Make every decision reproducible and bound resource usage.

**Inputs:** all intermediate requests, patches, logs, test reports, snapshots,
and Docker container/image identifiers.

**Outputs:** per-task/per-objective artifact directories and cleanup status.

Artifacts include `hunks.json`, `llm_request.json`, `llm_response.json`,
`llm_status.json`, `subsets.json`, `selected.patch`, F2P/P2P logs,
`counterfactuals`, `result.json`, and optional `final_snapshot`. Containers and
task images are removed before the next objective or task begins.

## End-to-end outputs

For each task/objective, PRUNE reports:

```text
original grouped hunk count
LLM subset count and rationales
candidate test outcomes
F2P and P2P pass counts
single/pair removal outcomes
behavioral versus infrastructure failures
final classification
validated snapshot path, if any
```

At the project level, a project is complete only when every objective has a
validated F2P/P2P-passing snapshot, with each objective starting from the
previous objective's finalized snapshot. Objective 1 is reported separately
when no prior objective-level P2P suite exists.

## Figure-generation brief

To convert this description into a figure, draw PRUNE as two nested loops. The
outer loop is the sequential project loop (`objective N -> Codex -> PRUNE ->
snapshot -> objective N+1`). The inner loop is the candidate-validation loop
(`extract hunks -> LLM subsets -> apply subset -> F2P/P2P -> counterfactual
removal -> classify`). Put the Codex agent and benchmark tests on opposite sides
of a trust-boundary line: the agent sees the task and repository, while the
validator alone sees F2P/P2P tests and ground truth. Use separate colors for
data artifacts, execution environments, model decisions, and test outcomes.
