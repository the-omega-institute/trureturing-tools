"""Select the CI archive for the requested branch or pull request head."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def api(path):
    endpoint = f"repos/{os.environ['GITHUB_REPOSITORY']}/{path}"
    return json.loads(subprocess.check_output(["gh", "api", endpoint]))


def source_head(source):
    pull = re.fullmatch(r"pr/([1-9][0-9]*)", source)
    if pull:
        head = api(f"pulls/{pull[1]}")["head"]
        return head["sha"], f"pr.{pull[1]}", head["ref"], head["repo"]["id"]
    ref = f"refs/heads/{source}"
    subprocess.run(["git", "check-ref-format", ref], check=True)
    sha, _ = subprocess.check_output(
        ["git", "ls-remote", "--exit-code", "--refs", "origin", ref], text=True
    ).split()
    channel = "dev" if source == "dev" else "feature"
    return sha, channel, source, int(os.environ["GITHUB_REPOSITORY_ID"])


def attempt_artifact(run):
    name = f"tools-release-{run['run_attempt']}"
    artifacts = api(f"actions/runs/{run['id']}/artifacts?name={name}")["artifacts"]
    matches = [a for a in artifacts if a["name"] == name and not a["expired"]]
    if len(matches) == 1:
        print(f"Using {run['html_url']} attempt {run['run_attempt']}")
        return matches[0]["id"]


def completed_dev_push(event, repo_id):
    run = event["workflow_run"]
    if (event["action"] != "completed" or run["status"] != "completed" or run["conclusion"] != "success"
            or run["event"] != "push" or run["head_branch"] != "dev"
            or run["repository"]["id"] != repo_id or (run["head_repository"] or {}).get("id") != repo_id
            or run["workflow_id"] != api("actions/workflows/ci.yml")["id"]):
        sys.exit("Expected successful same-repository dev push CI")
    # The dev CI package job depends on verify and the complete consumer matrix.
    artifact = attempt_artifact(run)
    if artifact is None:
        sys.exit("Expected one unexpired archive from the completed CI attempt")
    return run["head_sha"], artifact


def tested_archive(sha, branch, repo_id):
    runs = api(f"actions/workflows/ci.yml/runs?head_sha={sha}&status=success&per_page=100")
    expected_jobs = {"verify", "package", *(f"Consumer fixture {i}" for i in range(4))}
    for run in runs["workflow_runs"]:
        if (run["event"] not in {"push", "pull_request", "workflow_dispatch"}
                or run["head_sha"] != sha or run["head_branch"] != branch
                or (run["head_repository"] or {}).get("id") != repo_id):
            continue
        run_id = run["id"]
        jobs = api(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100")["jobs"]
        succeeded = {job["name"] for job in jobs if job["conclusion"] == "success"}
        if not expected_jobs <= succeeded:
            continue
        artifact = attempt_artifact(run)
        if artifact is not None:
            return artifact
    sys.exit(f"No successful tools CI archive for {branch} at {sha}. "
             "Run the Test tools workflow on the branch or re-run the pull request CI.")


def main():
    series = os.environ["SERIES"]
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", series):
        sys.exit("Invalid tools release series")
    if os.environ["GITHUB_EVENT_NAME"] == "workflow_run":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        sha, artifact = completed_dev_push(event, int(os.environ["GITHUB_REPOSITORY_ID"]))
        tag = f"v{series}-dev.g{sha}"
    else:
        sha, channel, branch, repo_id = source_head(os.environ["SOURCE"])
        artifact = tested_archive(sha, branch, repo_id)
        tag = f"v{series}-{channel}.{os.environ['GITHUB_RUN_NUMBER']}.g{sha[:12]}"
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit("Invalid tools source commit")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        print(f"sha={sha}\ntag={tag}\nartifact={artifact}", file=out)


if __name__ == "__main__":
    main()
