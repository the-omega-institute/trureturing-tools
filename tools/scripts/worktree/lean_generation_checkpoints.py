"""Discover partial immutable index receipts separately from the complete generation ref."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import lean_cache_release as host
from lean_generation_index import ConflictingArtifacts, validate
from lean_generation_remote import GitHub, InvalidIndexBody, MissingCheckpoint


def publisher():
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    if (repo != host.REPO or env.get("GITHUB_REF") != "refs/heads/dev"
            or env.get("GITHUB_REF_PROTECTED") != "true"
            or env.get("GITHUB_WORKFLOW_REF") != f"{repo}/.github/workflows/cache-release-publish.yml@refs/heads/dev"
            or env.get("GITHUB_EVENT_NAME") not in {"workflow_dispatch", "schedule"}
            or env.get("STRATALINT_CACHE_WRITES") != "true"):
        raise ValueError("only the protected generation publisher can stage a checkpoint")
    return {"run": env["GITHUB_RUN_ID"], "attempt": env["GITHUB_RUN_ATTEMPT"]}


def prefix(domain):
    if not re.fullmatch("[0-9a-f]{64}", domain):
        raise ValueError("invalid checkpoint dependency domain")
    return "tags/lean-checkpoint-v1-" + domain + "-"


def create_ref(remote, name, tag_sha):
    for attempt in range(3):
        try:
            return remote.api("git/refs", "-X", "POST", "-f", "ref=refs/tags/" + name, "-f", "sha=" + tag_sha)
        except host.GitHubCommandError as creation:
            diagnostic = (creation.stderr or "").strip()
            status = re.search(r"\bHTTP (\d{3})\b", diagnostic)
            code = int(status[1]) if status else 0
            throttled = code == 429 or (code == 403 and "rate limit" in diagnostic.lower())
            ambiguous = (diagnostic == "unexpected end of JSON input" or 500 <= code < 600 or throttled)
            if not ambiguous and code not in (409, 422):
                raise
            try:
                return remote.api("git/ref/tags/" + name)
            except host.GitHubCommandError as readback:
                if not re.search(r"\bHTTP 404\b", readback.stderr or ""):
                    raise
            delay = (60 if throttled else 2) * 2 ** attempt
            if not ambiguous or attempt == 2 or host.remaining(remote.deadline) <= delay:
                raise
            print(f"Retrying checkpoint ref {name} after {diagnostic} in {delay}s", file=sys.stderr)
            time.sleep(delay)


def save(remote, domain, receipt, directory):
    from lean_cache_generation import stage_indexes
    validate(receipt, complete=False)
    checkpoint = {**receipt, "producer": publisher()}
    digest = stage_indexes(remote, checkpoint, directory)
    name = prefix(domain).removeprefix("tags/") + digest
    keys = sorted(target["key"] for target in receipt["targets"].values())
    message = json.dumps(keys, separators=(",", ":"))
    tag = remote.api("git/tags", "-X", "POST", "-f", "tag=" + name, "-f", "message=" + message,
                     "-f", "type=commit", "-f", "object=" + receipt["source"])
    ref = create_ref(remote, name, tag["sha"])
    if ref["object"]["sha"] != tag["sha"]:
        stored = remote.api("git/tags/" + ref["object"]["sha"])
        if stored["message"] != message or stored["object"]["sha"] != receipt["source"]:
            raise ValueError("checkpoint metadata differs from the existing tag")

    # The durable index and its discovery ref already exist. Eviction, write
    # quota, or a transient cache-service outage cannot invalidate this batch.
    try:
        remote.ci("put", digest, Path(directory) / "index")
        if remote.get_index(digest, Path(directory) / "ci", "ci") != checkpoint:
            raise ValueError("checkpoint CI index readback differs")
    except subprocess.CalledProcessError as error:
        print(f"Lean checkpoint {digest} retained after CI cache failure ({error.returncode})", file=sys.stderr)


def descriptors(remote, domain):
    # Read candidate key metadata in pages. The actual partial index is fetched
    # only when this target checkout requests a key present in the batch.
    tag_prefix = prefix(domain).removeprefix("tags/")
    owner, repo = host.REPO.split("/")
    query = """query($owner:String!,$repo:String!,$prefix:String!,$cursor:String){
      repository(owner:$owner,name:$repo){
        refs(refPrefix:"refs/tags/",query:$prefix,first:100,after:$cursor){
          nodes{name target{... on Tag{message target{oid}}}}
          pageInfo{endCursor hasNextPage}
        }
      }
    }"""
    args = ["api", "graphql", "-f", "query=" + query, "-f", "owner=" + owner,
            "-f", "repo=" + repo, "-f", "prefix=" + tag_prefix]
    cursor = []
    while True:
        page = json.loads(host.gh(remote.deadline, *args, *cursor))["data"]["repository"]["refs"]
        for row in page["nodes"]:
            digest = row["name"].removeprefix(tag_prefix)
            if not re.fullmatch("[0-9a-f]{64}", digest):
                continue
            try:
                descriptor = row["target"]
                advertised = json.loads(descriptor["message"])
                source = descriptor["target"]["oid"]
                if (not re.fullmatch("[0-9a-f]{40}", source) or not isinstance(advertised, list)
                        or not advertised or not all(isinstance(k, str) and re.fullmatch("[0-9a-f]{64}", k)
                                                     for k in advertised)):
                    raise ValueError("malformed checkpoint descriptor")
            except (KeyError, ValueError, TypeError) as error:
                print(f"Skipping unusable checkpoint descriptor {digest}: {error}", file=sys.stderr)
                continue
            yield digest, source, advertised
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = ["-f", "cursor=" + page["pageInfo"]["endCursor"]]


def discover(remote, domain, keys=None):
    runs = {}
    with tempfile.TemporaryDirectory() as directory:
        for digest, source, advertised in descriptors(remote, domain):
            if keys is not None and keys.isdisjoint(advertised):
                continue
            try:
                checkpoint = remote.get_index(digest, Path(directory) / "checkpoint", "checkpoint")
            except (MissingCheckpoint, InvalidIndexBody) as error:
                print(f"Skipping unavailable checkpoint {digest}: {error}", file=sys.stderr)
                continue
            try:
                validate(checkpoint, complete=False)
                if sorted(advertised) != sorted(row["key"] for row in checkpoint["targets"].values()):
                    raise ValueError("checkpoint targets differ from its advertised keys")
            except ConflictingArtifacts:
                raise
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                print(f"Skipping unusable checkpoint receipt {digest}: {error}", file=sys.stderr)
                continue
            run_id = checkpoint["producer"]["run"]
            attempt = checkpoint["producer"]["attempt"]
            if not all(isinstance(n, str) and re.fullmatch(r"[1-9]\d*", n) for n in (run_id, attempt)):
                raise ValueError("invalid checkpoint producer")
            key = run_id, attempt
            if key not in runs:
                runs[key] = remote.api(f"actions/runs/{run_id}/attempts/{attempt}")
            run = runs[key]
            if (run["repository"]["full_name"] != host.REPO or run["head_branch"] != "dev"
                    or run["path"] != ".github/workflows/cache-release-publish.yml"
                    or run["event"] not in {"schedule", "workflow_dispatch"}
                    or checkpoint["source"] != source):
                raise ValueError("checkpoint did not originate in the protected dev publisher")
            yield checkpoint


def remote_for(root):
    return GitHub(root, host.operation_deadline())
