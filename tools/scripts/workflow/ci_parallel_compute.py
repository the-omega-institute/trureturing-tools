"""Share current's remaining wall-clock allowance among concurrent Lean report batches."""

import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import ci_compute


JUDGE = "Build the batch judge"
CHECK = "Check the Lean batch"
BATCH = re.compile(r"(?:.*/ )?Lean report batch ([0-9]+)")


def timestamp(value):
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("missing time zone in GitHub timestamp")
    return instant.timestamp()


def union_seconds(intervals):
    end = -math.inf
    elapsed = 0
    for start, stop in sorted(intervals):
        if stop < start:
            raise ValueError("invalid compute interval")
        elapsed += max(0, stop - max(end, start))
        end = max(end, stop)
    return elapsed


def intervals(job, epoch, now, final):
    """Bound unfinished work from its last confirmed preparation or compute step."""
    if job is None or not job.get("started_at"):
        if final:
            raise ValueError("missing Lean batch")
        return [(epoch, now)]
    if final and job.get("conclusion") != "success":
        raise ValueError("Lean batch did not succeed: " + job["name"])
    # A previous successful attempt can legitimately predate a freshly running sibling.
    frontier = timestamp(job["started_at"])
    if frontier > now:
        raise ValueError("job starts in the future")
    billed = []
    finished = set()
    for step in job.get("steps", []):
        name = step["name"]
        if name in (JUDGE, CHECK) and name in finished:
            raise ValueError("duplicate batch computation step")
        if step.get("status") != "completed":
            if name in (JUDGE, CHECK) and step.get("started_at"):
                frontier = timestamp(step["started_at"])
            break
        if name in (JUDGE, CHECK):
            result = step.get("conclusion")
            if result == "success" or (not final and result not in ("skipped", None)):
                # The service reports integer UTC seconds. Round completion outward.
                billed.append((timestamp(step["started_at"]), timestamp(step["completed_at"]) + 1))
            elif result != "skipped" or name != JUDGE:
                raise ValueError("missing or unsuccessful batch computation: " + name)
            finished.add(name)
        if step.get("completed_at"):
            frontier = max(frontier, timestamp(step["completed_at"]))
    if finished != {JUDGE, CHECK}:
        if final:
            raise ValueError("incomplete Lean batch compute evidence")
        billed.append((min(frontier, now), now))
    return billed


def allowance(jobs, count, remaining, epoch, now, final=False):
    if not 1 <= count <= 256 or not 0 < epoch <= now:
        raise ValueError("invalid batch count or start time")
    rows = {}
    for job in jobs:
        match = BATCH.fullmatch(job["name"])
        if not match:
            continue
        batch = int(match[1])
        if batch not in range(count) or batch in rows:
            raise ValueError("unexpected or duplicate Lean batch")
        rows[batch] = job
    billed = []
    for i in range(count):
        billed.extend(intervals(rows.get(i), epoch, now, final))
    return max(0, ci_compute.current_remainder(remaining) - union_seconds(billed))


def github_jobs():
    repo = os.environ["GITHUB_REPOSITORY"]
    run = os.environ["GITHUB_RUN_ID"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not run.isdigit():
        raise ValueError("invalid GitHub run identity")
    endpoint = f"repos/{repo}/actions/runs/{run}/jobs?filter=latest&per_page=100"
    pages = json.loads(subprocess.check_output(["gh", "api", "--paginate", "--slurp", endpoint]))
    return [job for page in pages for job in page["jobs"]]


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="operation", required=True)
    for name in ("run", "resume"):
        command = commands.add_parser(name)
        command.add_argument("--remaining", type=ci_compute.current_remainder, required=True)
        command.add_argument("--epoch", type=int, required=True)
        command.add_argument("--count", type=int, required=True)
        if name == "run":
            command.add_argument("argv", nargs=argparse.REMAINDER)
        else:
            command.add_argument("--budget-file", type=Path, required=True)
    args = cli.parse_args()
    final = args.operation == "resume"
    left = allowance(github_jobs(), args.count, args.remaining, args.epoch, time.time(), final)
    print(f"CI_PARALLEL_COMPUTE remaining_seconds={left:.3f}", file=sys.stderr, flush=True)
    if final:
        ci_compute.resume_current(args.budget_file, left)
        return 0
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        raise ValueError("missing batch computation command")
    if not left:
        return 124
    # While this command is active, the global union grows by one second per second.
    started = time.monotonic()
    env = {**os.environ, ci_compute.ACTIVE: "1"}
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    result = subprocess.call(["timeout", "--kill-after=5s", f"{left}s", *argv], env=env)
    return 124 if result == 0 and time.monotonic() - started >= left else result


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        print("CI_PARALLEL_COMPUTE_ERROR", error, file=sys.stderr)
        raise SystemExit(2)
