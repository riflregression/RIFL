# SafeMin Architecture and Algorithm

SafeMin is a FeatBench post-processing pipeline for reducing the size of an
already-generated feature patch. It does not generate a feature implementation
from an issue description. Instead, it takes a patch that has already passed
the benchmark's feature and regression tests, asks an LLM which patch hunks
may be sufficient, and validates those proposed subsets in a clean Docker
repository.

The implementation is in [`safemin.py`](./safemin.py). Its design has five
main responsibilities:

1. select eligible FeatBench tasks;
2. load and normalize the generated patch;
3. represent the patch as semantically grouped hunks;
4. ask an LLM for candidate hunk subsets; and
5. execute and counterfactually validate those subsets.

The result is a set of passing, one-hunk-minimal candidates together with the
test logs and intermediate artifacts needed to audit the decision.

## 1. Inputs and trust boundaries

SafeMin consumes three kinds of input.

### FeatBench task specification

The task specification comes from FeatBench and contains:

- `instance_id`;
- repository and base commit;
- natural-language feature request;
- the benchmark test patch;
- `FAIL_TO_PASS`, the feature tests; and
- `PASS_TO_PASS`, the regression tests.

The base repository is checked out at `base_commit` for every candidate.

### Original agent result

The current SafeMin implementation uses the saved Codex evaluation result to
determine eligible tasks and locate the generated `patch.diff`. A task is
eligible by default only when its latest saved result has both
`success_f2p == true` and `success_p2p == true`.

This is an important architectural boundary: SafeMin minimizes a previously
successful patch. It does not generate a new patch from the issue description.
The saved result is used to locate the artifact; the candidate itself is
re-applied and re-tested inside Docker.

### LLM response

The LLM receives the feature request and a compact description of each
candidate hunk. It must return strict JSON:

```json
{
  "subsets": [
    {
      "hunks": ["H0001", "H0003"],
      "rationale": "These hunks implement the required behavior together."
    }
  ]
}
```

Only known, non-empty hunk IDs are accepted. Unknown IDs, empty subsets, and
malformed responses are discarded before testing.

## 2. End-to-end data flow

```text
FeatBench specs + saved passing result
                  |
                  v
       task selection and deduplication
                  |
                  v
       generated patch.diff is loaded
                  |
                  v
       patch hunks are extracted/grouped
                  |
                  v
       source context is attached
                  |
                  v
       LLM proposes valid hunk subsets
                  |
                  v
       one task-specific Docker container
                  |
                  v
       candidate patch + benchmark test patch
                  |
                  v
       F2P and P2P execution
                  |
          +-------+-------+
          |               |
       failure        pass
          |               |
       discard       remove each hunk once
                          |
                          v
              counterfactual F2P/P2P runs
                          |
                          v
                retain one-minimal subsets
                          |
                          v
                 save artifacts and cleanup
```

The pipeline processes tasks sequentially. `DockerEnvironmentManager` creates
the task container and ensures the task image exists. A `finally` block removes
the container and calls `cleanup_task_image`, which removes both the local and
remote task-image tags. This bounds disk use and ensures a later task does not
inherit mutable state from an earlier task.

## 3. Hunk representation

SafeMin begins with the normalized unified diff returned by the FeatBench patch
analyzer. Each physical `@@` section is a raw diff section. Sections are then
grouped by:

```text
(filename, enclosing symbol, file status)
```

The resulting `Hunk` record contains:

| Field | Meaning |
| --- | --- |
| `id` | Stable local identifier such as `H0001` |
| `filename` | Repository-relative target file |
| `status` | Modified, added, or removed |
| `symbol` | Enclosing function, class, or file scope |
| `change_type` | Function, class, or statement |
| `patch_sections` | One or more physical diff sections |
| `context` | Diff plus optional enclosing definition |
| `import_only` | Whether all changed lines are imports |
| `is_test` | Whether the target appears to be a test file |

Grouping multiple physical sections under one symbol is deliberate. A function
modified in two separate locations should be treated as one semantic candidate
when the LLM reasons about dependencies.

### Symbol discovery

Symbol discovery uses two fallbacks:

1. inspect the unified-diff context for `def`, `async def`, or `class` lines;
2. if context is insufficient, parse the original source with Python `ast` and
   map the hunk's old line number to an enclosing definition.

If neither method succeeds, the hunk is assigned file scope.

### Import-only changes

Import-only hunks are removed from the LLM's choice set. They are automatically
included in every tested candidate because a selected code hunk may require an
import to parse or execute. This prevents the LLM from accidentally selecting
an implementation without its required import dependency.

## 4. Candidate patch construction

The LLM's hunk IDs are converted back into patch changes grouped by
`(filename, status)`. All sections belonging to a selected hunk are concatenated
in their original order. The patch is rendered as a standard unified diff and
stored before execution.

For every candidate, SafeMin performs these operations:

1. checkout the task's `base_commit`;
2. apply the candidate feature patch;
3. apply FeatBench's test patch;
4. run every F2P test;
5. run every P2P test; and
6. classify the candidate as `PASS` only if all expected tests pass.

The candidate is therefore evaluated against the old repository plus the
candidate feature patch, not against the already-modified answer checkout.

Formally, let:

- `A` be the original generated patch;
- `H(A)` be its grouped hunk set;
- `S ⊆ H(A)` be a proposed subset;
- `Apply(B, S)` be applying the selected changes to base repository `B`; and
- `Tests_F`, `Tests_P` be the F2P and P2P suites.

The validity predicate is:

```text
Valid(S) =
    Pass(Tests_F, Apply(B, S) + test_patch)
    AND
    Pass(Tests_P, Apply(B, S) + test_patch)
```

## 5. LLM subset-planning stage

The LLM is used as a dependency-aware proposal mechanism, not as the final
correctness oracle. The prompt contains:

- the feature request;
- hunk IDs;
- filenames;
- enclosing symbols;
- change types;
- test-file markers; and
- source/diff context.

The system instruction requires the LLM to return only valid hunk IDs and to
keep dependent hunks together. SafeMin sorts subsets by increasing cardinality:

```text
S1, S2, ... ordered by |S|
```

This lets the pipeline search small candidates first. As soon as at least one
minimal subset is found at a given size, larger sizes are not needed for
one-hunk minimality, but every candidate at the first passing size is still
evaluated.

## 6. Minimality algorithm

SafeMin uses a two-stage minimization strategy:

1. the LLM proposes candidate subsets;
2. the executor verifies each proposal and tests the removal of every selected
   hunk.

A passing subset `S` is reported as valid-minimal when no single selected hunk
can be removed while preserving both test suites:

```text
Minimal(S) = Valid(S)
              AND
              ∀ h ∈ S: NOT Valid(S \\ {h})
```

This is one-minimality, not necessarily global minimum. A smaller subset may
require removing two dependent hunks together, which single-hunk removal does
not discover. The LLM's proposed subset sizes provide the practical search over
larger combinations.

### Algorithm

```text
algorithm SafeMin(task, original_result, config):
    spec ← load FeatBench task specification

    if original_result.f2p != PASS or original_result.p2p != PASS:
        return SKIP

    patch ← load patch.diff from original_result
    base ← spec.base_commit
    hunks ← ExtractAndGroupHunks(patch, source_at(base))

    imports ← {h ∈ hunks | h.import_only}
    candidates ← hunks − imports
    proposals ← LLMSelectSubsets(spec.problem_statement, candidates)
    proposals ← ValidateIDsAndSortBySize(proposals)

    minimal ← []

    for size in distinct_sizes(proposals):
        same_size ← proposals where |proposal.hunks| = size

        for proposal in same_size:
            selected ← imports ∪ proposal.hunks
            result ← RunPair(spec, base, selected)

            if result is not PASS:
                save(result)
                continue

            all_removals_fail ← true
            for h in selected where h is not import_only:
                counterfactual ← RunPair(spec, base, selected − {h})
                save(counterfactual)
                if counterfactual is PASS:
                    all_removals_fail ← false

            if all_removals_fail:
                minimal.append(proposal)

        if minimal is not empty:
            break

    save(task, hunks, proposals, evaluations, minimal)
    cleanup_container_and_task_image()
    return minimal
```

### Complexity

Let:

- `p` be the number of LLM-proposed subsets tested;
- `k` be the number of selected non-import hunks in a passing subset; and
- `T` be the cost of running the F2P and P2P suites.

The dominant test cost is approximately:

```text
O((p + Σ k over passing proposals) × T)
```

The first term tests proposals. The second term tests each one-hunk
counterfactual for every passing proposal. Patch extraction, grouping, prompt
construction, and JSON validation are comparatively small relative to Docker
test execution.

## 7. Docker and resource lifecycle

For each task:

1. `DockerEnvironmentManager.create_container` checks for a stale cached
   container and removes it if present;
2. the task image is pulled or reused;
3. one container is created from that image;
4. all candidate and counterfactual tests for that task run in the container;
5. the container is forcibly removed in `finally`; and
6. both task-image tags are removed in `finally`.

The cleanup is also performed if LLM request/response handling fails before
candidate evaluation begins. This is important because image downloads and
containers are the largest persistent resources in a smoke run.

## 8. Storage and observability

`ProgressTable` maintains a durable global progress file and one per-task
progress file. It records:

- current stage;
- original hunk count;
- candidate hunk count;
- subsets tested;
- passing subsets; and
- final minimal subset sizes.

Per-task artifacts are written below the configured output directory:

```text
smoke-results/safemin/
├── progress.json
└── <instance-id>/
    ├── progress.json
    ├── spec.json
    ├── hunks.json
    ├── filtered_hunks.json
    ├── llm_request.json
    ├── llm_response.json
    ├── subsets.json
    ├── subset-<size>-<ordinal>.patch.diff
    ├── subset-<size>-<ordinal>.json
    ├── subset-<size>-<ordinal>-remove-Hxxxx.patch.diff
    ├── subset-<size>-<ordinal>-remove-Hxxxx.json
    └── result.json
```

If a task fails outside the normal result path, `error.txt` is written and the
progress row is marked `error`. The normal `--resume` mode skips tasks with an
existing result artifact. The current implementation also checks historical
SafeMin output roots when resuming, so users should select a new output
directory when they want a completely independent experiment.

## 9. Operational modes

```sh
# Use the default config and eligible passing tasks.
python rifl/safemin.py

# Process a bounded cohort.
python rifl/safemin.py --limit 10 \
  --output smoke-results/safemin-10

# Select explicit tasks.
python rifl/safemin.py \
  --task-id projectmesa__mesa-2219 \
  --output smoke-results/safemin-one

# Inspect selection without making an LLM request.
python rifl/safemin.py --dry-run --limit 10

# Resume a saved SafeMin output directory.
python rifl/safemin.py --resume \
  --output smoke-results/safemin-10
```

The configuration controls the model, API-key environment variable, output
directory, and test timeout. Credentials should be supplied through the
environment; they should not be committed to a configuration file.

## 10. Guarantees and limitations

SafeMin provides these practical guarantees:

- candidate patches are always derived from the generated patch;
- candidates are applied to the original base commit;
- accepted candidates pass both F2P and P2P according to the configured test
  oracle;
- reported candidates are one-hunk-minimal with respect to the tested subset;
- every candidate and counterfactual has persisted patch and test artifacts;
- Docker resources are cleaned up after each task.

It does not guarantee:

- the globally smallest behavior-preserving patch;
- correctness beyond the available FeatBench tests;
- that the LLM's rationale is correct;
- that two hunks which must be removed together will be discovered by the
  one-hunk counterfactual pass; or
- that a prior agent patch is independently reproducible.

The central design tradeoff is explicit: use an LLM to propose semantically
plausible small subsets, then use isolated execution—not the LLM rationale—as
the correctness and minimality oracle.


