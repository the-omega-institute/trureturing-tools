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
        pr = api(f"pulls/{pull[1]}")
        if (pr["base"]["repo"]["id"] != int(os.environ["GITHUB_REPOSITORY_ID"])
                or not pr["head"]["repo"]):
            sys.exit("Expected a PR in the canonical tools repository")
        head = pr["head"]
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


def ci_jobs(workflow):
    return json.loads(subprocess.check_output(
        ["ruby", "-ryaml", "-rjson", "-e", "puts JSON.generate(YAML.safe_load(STDIN.read).fetch('jobs'))"],
        input=workflow,
    ))


def ci_contract():
    jobs = ci_jobs(Path(__file__).resolve().parents[1].joinpath("workflows/ci.yml").read_bytes())
    required = {}
    pending = ["package"]
    while pending:
        key = pending.pop()
        if key not in required:
            required[key] = jobs[key]
            needs = jobs[key].get("needs", [])
            pending.extend([needs] if isinstance(needs, str) else needs)
    return required


def verified_artifact(run, contract):
    sha = run["head_sha"]
    expected = set()
    for key, job in contract.items():
        matrix = job.get("strategy", {}).get("matrix")
        if matrix:
            if set(matrix) != {"shard"} or "${{ matrix.shard }}" not in job["name"]:
                sys.exit("Unsupported matrix in the trusted tools CI contract")
            expected.update(job["name"].replace("${{ matrix.shard }}", str(shard)) for shard in matrix["shard"])
        else:
            expected.add(job.get("name", key))
    run_id, attempt = run["id"], run["run_attempt"]
    jobs = api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")["jobs"]
    succeeded = {job["name"] for job in jobs if job["conclusion"] == "success" and job["head_sha"] == sha}
    if expected <= succeeded:
        return attempt_artifact(run)


def completed_dev_ci(event, repo_id):
    run = event["workflow_run"]
    if (event["action"] != "completed" or run["status"] != "completed" or run["conclusion"] != "success"
            or run["event"] not in {"push", "workflow_dispatch"} or run["head_branch"] != "dev"
            or run["repository"]["id"] != repo_id or (run["head_repository"] or {}).get("id") != repo_id
            or run["workflow_id"] != api("actions/workflows/ci.yml")["id"]):
        sys.exit("Expected successful same-repository dev CI")
    artifact = verified_artifact(run, ci_contract())
    if artifact is None:
        sys.exit("Expected one unexpired archive from the completed CI attempt")
    return run["head_sha"], artifact


def tested_archive(sha, branch, repo_id, pull_number=None):
    runs = api(f"actions/workflows/ci.yml/runs?head_sha={sha}&status=success&per_page=100")
    contract = ci_contract()
    for run in runs["workflow_runs"]:
        if (run["event"] not in {"push", "pull_request", "workflow_dispatch"}
                or run["head_sha"] != sha or run["head_branch"] != branch
                or (run["head_repository"] or {}).get("id") != repo_id
                or run["status"] != "completed" or run["conclusion"] != "success"):
            continue
        if run["event"] == "pull_request" and pull_number and run["pull_requests"] and not any(
                pull["number"] == pull_number
                for pull in run["pull_requests"]):
            continue
        artifact = verified_artifact(run, contract)
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
        sha, artifact = completed_dev_ci(event, int(os.environ["GITHUB_REPOSITORY_ID"]))
        tag = f"v{series}-dev.g{sha}"
    else:
        sha, channel, branch, repo_id = source_head(os.environ["SOURCE"])
        pull_number = int(channel.removeprefix("pr.")) if channel.startswith("pr.") else None
        artifact = tested_archive(sha, branch, repo_id, pull_number)
        tag = (f"v{series}-dev.g{sha}" if channel == "dev"
               else f"v{series}-{channel}.{os.environ['GITHUB_RUN_NUMBER']}.g{sha[:12]}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        sys.exit("Invalid tools source commit")
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
        print(f"sha={sha}\ntag={tag}\nartifact={artifact}", file=out)


if __name__ == "__main__":
    main()
