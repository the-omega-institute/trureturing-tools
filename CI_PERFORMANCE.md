# CI performance targets

The tools CI measures wall-clock time inside `test-suites.py` and emits one
`CI_PERFORMANCE` record per verification or consumer-fixture job. The record
contains the measured duration, the target, the hard budget and the resulting
status. GitHub step summaries retain the same values for each run.

The acceptance statistic is the nearest-rank P90 over twelve successful runs.
The current targets are

- standalone verification at or below 300 seconds, with a 420-second budget
- ordinary consumer fixtures at or below 600 seconds, with a 960-second budget
- the `frozen` consumer fixture at or below 780 seconds, with a 960-second budget

The reference window measured 12 successful runs. Its P90 was 351 seconds for
standalone verification and 882 seconds for the `frozen` fixture. A target is
met when the next complete window is at or below its target. A budget breach
fails the job through the workflow timeout and is reported separately from a
target miss.

The workflow cancels obsolete runs for the same commit, restores NuGet and
Lean dependencies, and reuses the completed `StrataLint.TestEvidence` build
across project-level test invocations. These are the required optimization
mechanisms for the targets above.
