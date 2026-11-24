"""Discover partial immutable index receipts separately from the complete generation ref."""

import os
from pathlib import Path
import re
import tempfile

import lean_cache_release as host
from lean_generation_index import validate
from lean_generation_remote import GitHub


def publisher():
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    if (repo != host.REPO or env.get("GITHUB_REF") != "refs/heads/dev"
            or env.get("GITHUB_REF_PROTECTED") != "true"
            or env.get("GITHUB_WORKFLOW_REF") != f"{repo}/.github/workflows/lean-cache-publish.yml@refs/heads/dev"
            or env.get("GITHUB_EVENT_NAME") not in {"workflow_dispatch", "schedule"}
            or env.get("STRATALINT_CACHE_WRITES") != "true"):
        raise ValueError("only the protected generation publisher can stage a checkpoint")
    return {"run": env["GITHUB_RUN_ID"], "attempt": env["GITHUB_RUN_ATTEMPT"]}


def prefix(domain):
    if not re.fullmatch("[0-9a-f]{64}", domain):
        raise ValueError("invalid checkpoint dependency domain")
    return "tags/lean-checkpoint-v1-" + domain + "-"


def save(remote, domain, receipt, directory):
    from lean_cache_generation import stage_indexes
    validate(receipt, complete=False)
    checkpoint = {**receipt, "producer": publisher()}
    digest = stage_indexes(remote, checkpoint, directory)
    ref = prefix(domain) + digest
    try:
        result = remote.api("git/refs", "-X", "POST", "-f", "ref=refs/" + ref, "-f", "sha=" + receipt["source"])
    except host.GitHubCommandError:
        result = remote.api("git/ref/" + ref)
    if result["object"]["sha"] != receipt["source"]:
        raise ValueError("checkpoint ref belongs to a different source")


def discover(remote, domain):
    ref_prefix = prefix(domain)
    refs = remote.api("git/matching-refs/" + ref_prefix)
    runs = {}
    with tempfile.TemporaryDirectory() as directory:
        for ref in refs:
            digest = ref["ref"].removeprefix("refs/" + ref_prefix)
            if not re.fullmatch("[0-9a-f]{64}", digest):
                raise ValueError("invalid checkpoint ref")
            checkpoint = remote.get_index(digest, Path(directory) / "checkpoint", "release")
            validate(checkpoint, complete=False)
            run_id = checkpoint["producer"]["run"]
            attempt = checkpoint["producer"]["attempt"]
            if not all(isinstance(n, str) and re.fullmatch(r"[1-9]\d*", n) for n in (run_id, attempt)):
                raise ValueError("invalid checkpoint producer")
            key = run_id, attempt
            if key not in runs:
                runs[key] = remote.api(f"actions/runs/{run_id}/attempts/{attempt}")
            run = runs[key]
            if (run["repository"]["full_name"] != host.REPO or run["head_branch"] != "dev"
                    or run["path"] != ".github/workflows/lean-cache-publish.yml"
                    or run["event"] not in {"schedule", "workflow_dispatch"}
                    or checkpoint["source"] != ref["object"]["sha"]):
                raise ValueError("checkpoint did not originate in the protected dev publisher")
            yield checkpoint


def remote_for(root):
    return GitHub(root, host.operation_deadline())
