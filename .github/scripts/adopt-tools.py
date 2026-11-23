"""Merge the exact tools PR selected by a merged consumer dev PR."""

import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools/consumer"))
from paired import (  # noqa: E402
    CONSUMER, PIN, PR_PREVIEW, SHA, TOOLS, api, base_trailers, branch_tip, contains,
    read_pin, tag_source, tools_dev_pull,
)
from pin import release_pin  # noqa: E402


def paired_number(pin):
    match = PR_PREVIEW.fullmatch(pin.get("version", ""))
    if match and re.fullmatch(SHA, pin.get("source_sha", "")) and pin["source_sha"].startswith(match[2]):
        return int(match[1])
    return None


def merged_tools_head(pull, source, dev):
    merge = pull["merge_commit_sha"]
    if not pull["merged"] or not merge or not contains(TOOLS, merge, dev):
        return False
    parents = api(TOOLS, f"git/commits/{merge}")["parents"]
    return len(parents) == 2 and parents[1]["sha"] == source


def adopt(number, expected_merge=None):
    consumer = api(CONSUMER, f"pulls/{number}")
    merge = consumer["merge_commit_sha"]
    if (consumer["base"]["ref"] != "dev" or consumer["base"]["repo"]["full_name"] != CONSUMER
            or not consumer["merged"] or not merge or (expected_merge and merge != expected_merge)
            or not contains(CONSUMER, merge, branch_tip(CONSUMER))):
        raise ValueError(f"consumer PR #{number} must have merged into {CONSUMER}/dev")

    blob, pin = read_pin(merge)
    tools_number = paired_number(pin)
    if tools_number is None:
        print(f"Consumer PR #{number} has no paired pin transition.")
        return False
    first_parent = api(CONSUMER, f"git/commits/{merge}")["parents"][0]["sha"]
    baseline, previous = read_pin(first_parent)
    if blob == baseline:
        print(f"Consumer PR #{number} has no paired pin transition.")
        return False

    pull = api(TOOLS, f"pulls/{tools_number}")
    source = pin["source_sha"]
    if not tools_dev_pull(pull) or pull["head"]["sha"] != source:
        raise ValueError(f"tools PR #{tools_number} moved from the pinned head or target")
    if release_pin(pin["version"], published_by=consumer["merged_at"]) != {
        k: v for k, v in pin.items() if k != "workflow_sha"
    }:
        raise ValueError("the published tools preview differs from the merged pin")
    if base_trailers(source) != [baseline]:
        raise ValueError("tools pin conflict: the preview is based on a different consumer pin")
    prior_source = tag_source(previous["version"])
    if previous.get("source_sha", prior_source) != prior_source or not contains(TOOLS, prior_source, source):
        raise ValueError("the pinned tools PR head lacks the previous consumer pin source")

    tools_dev = branch_tip(TOOLS)
    if merged_tools_head(pull, source, tools_dev):
        print(f"Tools PR #{tools_number} already merged the pinned head.")
        return False
    if pull["state"] != "open":
        raise ValueError(f"tools PR #{tools_number} must be open at the pinned head")
    if paired_number(previous) and not contains(TOOLS, prior_source, tools_dev):
        raise ValueError("the previous consumer pin source must reach tools/dev first")

    # Re-read the target immediately before the API's conditional head update.
    pull = api(TOOLS, f"pulls/{tools_number}")
    if not tools_dev_pull(pull) or pull["head"]["sha"] != source:
        raise ValueError(f"tools PR #{tools_number} moved from the pinned head or target")
    try:
        result = api(TOOLS, f"pulls/{tools_number}/merge", "-X", "PUT",
                     "-f", f"sha={source}", "-f", "merge_method=merge",
                     "-f", f"commit_message=Paired consumer PR: {CONSUMER}#{number} at {merge}")
        if not result["merged"]:
            raise ValueError(result["message"])
    except (subprocess.CalledProcessError, ValueError):
        pull = api(TOOLS, f"pulls/{tools_number}")
        if (not tools_dev_pull(pull) or pull["head"]["sha"] != source
                or not merged_tools_head(pull, source, branch_tip(TOOLS))):
            raise
    else:
        pull = api(TOOLS, f"pulls/{tools_number}")
        if (not tools_dev_pull(pull) or pull["head"]["sha"] != source
                or not merged_tools_head(pull, source, branch_tip(TOOLS))):
            raise ValueError(f"tools PR #{tools_number} did not land at the expected dev target")
    print(f"Merged tools PR #{tools_number} at {source} for consumer PR #{number}.")
    return True


def git(repo, *args):
    return subprocess.check_output(["git", "-C", repo, *args], text=True).strip()


def pin_transitions(repo):
    return git(repo, "log", "--first-parent", "--format=%H", "--reverse", "FETCH_HEAD", "--", PIN).splitlines()


def catch_up():
    pages = api(TOOLS, "pulls?state=open&base=dev&per_page=100", "--paginate", "--slurp")
    heads = {(pull["number"], pull["head"]["sha"]): pull["created_at"]
             for page in pages for pull in page
             if tools_dev_pull(pull) and base_trailers(pull["head"]["sha"])}
    if not heads:
        return
    # A PR publication cannot predate the named tools PR.
    since = datetime.fromisoformat(min(heads.values())) - timedelta(days=2)
    consumer_dev = branch_tip(CONSUMER)
    tip = api(CONSUMER, f"git/commits/{consumer_dev}")
    if datetime.fromisoformat(tip["committer"]["date"]) < since:
        return
    with tempfile.TemporaryDirectory() as repo:
        git(repo, "init", "--bare", "--quiet")
        git(repo, "remote", "add", "origin", f"https://github.com/{CONSUMER}.git")
        git(repo, "fetch", "--quiet", "--no-tags", "--filter=blob:none", f"--shallow-since={since.isoformat()}",
            "origin", consumer_dev)
        for merge in pin_transitions(repo):
            pin = json.loads(git(repo, "show", f"{merge}:{PIN}"))
            if (paired_number(pin), pin.get("source_sha")) not in heads:
                continue
            pulls = api(CONSUMER, f"commits/{merge}/pulls?per_page=100")
            for pull in pulls:
                if pull["merged_at"] and pull["merge_commit_sha"] == merge and pull["base"]["ref"] == "dev":
                    try:
                        adopt(pull["number"], merge)
                    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                        print(f"::error::Consumer PR #{pull['number']}: {error}", file=sys.stderr)
                        yield False
                    else:
                        yield True


def recover_dev_ci():
    dev = branch_tip(TOOLS)
    commit = api(TOOLS, f"git/commits/{dev}")
    if not re.search(rf"(?m)^Paired consumer PR: {re.escape(CONSUMER)}#[1-9][0-9]* at {SHA}$",
                     commit["message"]):
        return
    runs = api(TOOLS, f"actions/workflows/ci.yml/runs?branch=dev&head_sha={dev}&per_page=100")["workflow_runs"]
    if not any(run["event"] in {"push", "workflow_dispatch"} for run in runs):
        api(TOOLS, "actions/workflows/ci.yml/dispatches", "-f", "ref=dev")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("consumer_pr", nargs="?")
    args = parser.parse_args()
    if os.environ.get("GITHUB_REPOSITORY") != TOOLS or os.environ.get("GITHUB_REF") != "refs/heads/dev":
        parser.error("run from the tools dev workflow")
    try:
        try:
            if args.consumer_pr:
                if not re.fullmatch(r"[1-9][0-9]*", args.consumer_pr):
                    parser.error("expected a consumer PR number")
                adopt(int(args.consumer_pr))
            elif not all(list(catch_up())):
                raise ValueError("one or more paired tools merges failed")
        finally:
            recover_dev_ci()
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"tools adoption: {error}\n")


if __name__ == "__main__":
    main()
